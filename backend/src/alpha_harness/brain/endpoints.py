"""Typed wrappers over the BRAIN endpoint surface.

One place that knows which endpoint needs which ``Accept`` version and which ones are
asynchronous jobs.

Structured platform entities come back as Pydantic models. Two kinds of response stay raw
dicts: bulk reads where validating every row costs too much (``list_data_fields_all``), and
open-ended or undocumented blobs the callers read selectively.
"""

from typing import TYPE_CHECKING, Any

import structlog

from .altcha import Challenge
from .errors import BrainError, BrainServiceUnavailable, BrainVerificationRequired
from .platform import ALL_FIELDS_CEILING, ALL_FIELDS_PAGE
from .schemas import (
    BULK_FIELDS,
    BULK_FIELDS_ENVELOPE,
    Alpha,
    AuthState,
    BulkField,
    DataSet,
    Operator,
    RecordSet,
    SimulationRequest,
)

if TYPE_CHECKING:
    from .client import BrainClient, BrainResponse
    from .filters import AlphaQuery

log = structlog.get_logger(__name__)

# Endpoints pinned to a non-default Accept version.
V_SETTINGS_SCHEMA = "4.0"  # OPTIONS /simulations
#: GET /data-fields with all four scope params. Unpaginated in practice: tens of thousands
#: of rows arrive in one response.
V_FIELDS_ALL = "3.0"
V_ALPHA_LIST = "4.0"  # GET /users/{id}/alphas
#: GET /users/self/alphas/summary. Undocumented; 4.0 returns {unsubmitted, active,
#: decommissioned} where 2.0 returns {is, os, prod}.
V_ALPHA_SUMMARY = "4.0"

#: Daily submitted-Alpha counts, the series behind BRAIN's own "Submitted Alphas".
SUBMISSIONS_PATH = "/users/self/activities/submissions"

#: The simulation types this application sends; their per-type settings trees are merged in.
#: Region-agnostic is included because its only region, ``ALL``, appears nowhere else — so
#: merging the two trees is what makes "all regions at once" a market a user can choose.
SIMULATION_TYPES = ("REGULAR", "REGION_AGNOSTIC")


def _fold(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge one simulation type's settings tree into the schema built so far.

    A setting the base has not seen is taken whole; one it has keeps its own description and
    gains the other's choices.
    """
    merged = dict(base)
    for name, node in overrides.items():
        held = merged.get(name)
        if not isinstance(held, dict) or not isinstance(node, dict):
            merged[name] = node
            continue
        merged[name] = held | {"choices": _union(held.get("choices"), node.get("choices"))}
    return merged


def _union(held: Any, other: Any) -> Any:
    """Combine two ``choices`` trees, which are dependency maps ending in lists.

    Dependency maps merge key by key, so a type that only offers region ``ALL`` adds that one
    branch and leaves every other region's universes alone.
    """
    if isinstance(held, dict) and isinstance(other, dict):
        return {k: _union(held.get(k), other.get(k)) for k in {**held, **other}}
    if isinstance(held, list) and isinstance(other, list):
        by_value = {c.get("value") if isinstance(c, dict) else c: c for c in held}
        extra = [c for c in other if (c.get("value") if isinstance(c, dict) else c) not in by_value]
        return [*held, *extra]
    return other if held is None else held


class BrainEndpoints:
    """The BRAIN API, typed."""

    def __init__(self, client: BrainClient) -> None:
        self.client = client

    # -- authentication ------------------------------------------------

    async def get_captcha(self) -> Challenge:
        """Fetch an ALTCHA proof-of-work challenge."""
        r = await self.client.request("GET", "/captcha")
        if not isinstance(r.body, dict):
            raise ValueError(f"Unexpected captcha payload: {r.body!r}")
        return Challenge.from_payload(r.body)

    async def authenticate(self, email: str, password: str, *, captcha: str) -> AuthState:
        """Exchange Basic auth plus the solved captcha for a session cookie."""
        r = await self.client.request(
            "POST",
            "/authentication",
            json_body={"captcha": captcha},
            auth=(email, password),
        )
        return AuthState.model_validate(r.body or {})

    async def get_auth(self) -> AuthState | None:
        """Cheapest liveness check. ``None`` means no active session."""
        r = await self.client.request("GET", "/authentication", raise_for_status=False)
        if r.status == 401:
            error = self.client.to_error("GET", "/authentication", r)
            if isinstance(error, BrainVerificationRequired):
                # Not "no session": the session needs a browser check first.
                raise error
            return None
        if r.status == 204:
            return None
        if r.status == 429 or r.status >= 500:
            # Throttled or down, not signed out: callers must not drop the session for it.
            raise self.client.to_error("GET", "/authentication", r)
        if r.status >= 400 or not isinstance(r.body, dict):
            return None
        return AuthState.model_validate(r.body)

    async def logout(self) -> None:
        """End the session on BRAIN when it can be reached, and here whether or not."""
        try:
            await self.client.request("DELETE", "/authentication", raise_for_status=False)
        except BrainError as exc:
            # Offline: BRAIN lets the session lapse on its own; signing out must not wait for it.
            log.info("brain.logout_unsent", error=str(exc))
        finally:
            self.client.clear_cookies()

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """Fetch user profile details from /users/{user_id}."""
        r = await self.client.request("GET", f"/users/{user_id}", raise_for_status=False)
        if r.status >= 400 or not isinstance(r.body, dict):
            return {}
        return r.body

    # -- platform metadata ----------------------------------------------

    async def settings_schema(self) -> dict[str, Any]:
        """``OPTIONS /simulations`` at 4.0, resolved for the types this application sends.

        ``actions.POST.settings.children`` is the common tree; each type's
        ``settings.children`` overrides it, and region, universe, delay and neutralization
        live only there.

        The region-agnostic tree is folded into the same schema rather than kept apart:
        ``universe``, ``delay`` and ``neutralization`` are keyed *by region*, so it only adds
        an ``ALL`` branch to each, and region ``ALL`` then reads like any other market to
        every form, lab and validator downstream.
        """
        r = await self.client.request("OPTIONS", "/simulations", version=V_SETTINGS_SCHEMA)
        body = r.body if isinstance(r.body, dict) else {}
        post = body.get("actions", {}).get("POST", {})
        common = post.get("settings", {}).get("children", {})
        merged: dict[str, Any] = dict(common) if isinstance(common, dict) else {}
        by_type = {
            choice.get("value"): (choice.get("settings") or {}).get("children")
            for choice in post.get("type", {}).get("choices") or []
            if isinstance(choice, dict)
        }
        for name in SIMULATION_TYPES:
            overrides = by_type.get(name)
            if isinstance(overrides, dict):
                merged = _fold(merged, overrides)
        return merged

    async def list_operators(self) -> list[Operator]:
        """Every operator with signature and category — the language reference."""
        r = await self.client.request("GET", "/operators")
        raw = r.body if isinstance(r.body, list) else []
        return [Operator.model_validate(o) for o in raw]

    # -- simulations -----------------------------------------------------

    async def create_simulation(
        self, payload: SimulationRequest | list[SimulationRequest]
    ) -> BrainResponse:
        """Start a simulation. Returns the raw response — the caller needs ``Location``.

        A ``201`` carries the simulation id **only** in the ``Location`` header, and for a
        multi-simulation that is the *parent* id, the only handle that can cancel the
        batch — so persist it before doing anything else. Parsing is left to
        :mod:`alpha_harness.engine.tracker`, which owns the ordering that keeps cancellation
        safe.
        """
        if isinstance(payload, list):
            body: Any = [p.to_wire() for p in payload]
        else:
            body = payload.to_wire()
        r = await self.client.request("POST", "/simulations", json_body=body)
        if r.status != 201:
            # Only a 201 started a simulation; a redirect (say, to sign-in) did not.
            raise BrainServiceUnavailable(f"POST /simulations answered {r.status}", status=r.status)
        return r

    async def read_simulation(self, simulation_id: str) -> BrainResponse:
        """One raw status read, errors included: the tracker decides what each one means."""
        return await self.client.request(
            "GET", f"/simulations/{simulation_id}", raise_for_status=False
        )

    async def cancel_simulation(self, simulation_id: str) -> bool:
        """Cancel a queued or running simulation.

        Returns ``False`` if the platform no longer knows the id, or refused because it
        already finished: that refusal is a ``200`` whose body is a list of messages
        (``["Can not delete complete simulations"]``), not an error status.
        """
        r = await self.client.request(
            "DELETE", f"/simulations/{simulation_id}", raise_for_status=False
        )
        return r.status < 400 and not (isinstance(r.body, list) and r.body)

    # -- alphas ----------------------------------------------------------

    async def get_alpha(self, alpha_id: str) -> Alpha:
        r = await self.client.request("GET", f"/alphas/{alpha_id}")
        return Alpha.model_validate(r.body or {"id": alpha_id})

    async def get_recordset(self, alpha_id: str, name: str) -> RecordSet:
        """Fetch a time series. Asynchronous — goes through the Retry-After protocol."""
        r = await self.client.poll(f"/alphas/{alpha_id}/recordsets/{name}")
        return RecordSet.model_validate(r.body or {})

    async def check_alpha(self, alpha_id: str) -> dict[str, Any]:
        """Re-run submission checks without submitting. Asynchronous."""
        r = await self.client.poll(f"/alphas/{alpha_id}/check")
        return r.body if isinstance(r.body, dict) else {}

    async def correlations(self, alpha_id: str, kind: str = "self") -> dict[str, Any]:
        """``self`` or ``prod`` correlation. Asynchronous.

        ``power-pool`` is undocumented but works.
        """
        r = await self.client.poll(f"/alphas/{alpha_id}/correlations/{kind}")
        return r.body if isinstance(r.body, dict) else {}

    async def alpha_body(self, alpha_id: str) -> dict[str, Any]:
        """``GET /alphas/{id}`` exactly as sent: the page reads blocks the model leaves out
        (``is.investabilityConstrained``, ``classifications``)."""
        r = await self.client.request("GET", f"/alphas/{alpha_id}")
        return r.body if isinstance(r.body, dict) else {}

    async def recordset_body(self, alpha_id: str, name: str) -> dict[str, Any]:
        r = await self.client.poll(f"/alphas/{alpha_id}/recordsets/{name}")
        return r.body if isinstance(r.body, dict) else {}

    async def before_and_after(self, alpha_id: str) -> dict[str, Any]:
        """The pool's stats before and after adding this Alpha.

        Asynchronous: answers with ``Retry-After`` first.
        """
        r = await self.client.poll(f"/users/self/alphas/{alpha_id}/before-and-after-performance")
        return r.body if isinstance(r.body, dict) else {}

    async def update_alpha(self, alpha_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /alphas/{id}``: name, category, color, tags, description. Never submits."""
        r = await self.client.request("PATCH", f"/alphas/{alpha_id}", json_body=properties)
        return r.body if isinstance(r.body, dict) else {}

    # -- the alpha pool --------------------------------------------------
    #
    # Listing needs ``version=4.0`` and the filter DSL of
    # :mod:`alpha_harness.brain.filters`, appended to the path rather than passed as a
    # params dict — a dict helper encodes the operator into the value and the server then
    # matches nothing.
    #
    # There is deliberately no ``submit`` here: submission is irreversible, and the absence
    # of the method is the guarantee that no code path reaches it by mistake.

    async def list_alphas(self, query: AlphaQuery) -> dict[str, Any]:
        """One page of your alphas, with the total match count."""
        r = await self.client.request("GET", query.path(), version=V_ALPHA_LIST)
        body = r.body if isinstance(r.body, dict) else {}
        return {
            "count": int(body.get("count") or 0),
            "results": body.get("results") or [],
            "limit": query.limit,
            "offset": query.offset,
        }

    async def alphas_summary(self) -> dict[str, Any]:
        """Aggregate counts: ``{unsubmitted, active, decommissioned}``."""
        r = await self.client.request("GET", "/users/self/alphas/summary", version=V_ALPHA_SUMMARY)
        return r.body if isinstance(r.body, dict) else {}

    # -- competitions ----------------------------------------------------

    async def competitions(self) -> list[dict[str, Any]]:
        """Every competition, past and ongoing, with this account's ``status`` in each."""
        r = await self.client.request("GET", "/competitions")
        body = r.body if isinstance(r.body, dict) else {}
        return list(body.get("results") or [])

    async def competition(self, competition_id: str) -> dict[str, Any]:
        """One competition. Only this carries the account's ``leaderboard`` standing."""
        r = await self.client.request("GET", f"/competitions/{competition_id}")
        return r.body if isinstance(r.body, dict) else {}

    # -- data catalog ----------------------------------------------------

    #: A market's fields are ~40 MB, and eight scopes download at once. Measured on a first
    #: sync: every USA delay-1 scope (76,519 fields) blew the client's ordinary 30 s read and
    #: only landed on its fourth attempt — ``fetch_s=114`` for a transfer that succeeds in
    #: about forty. The retries were pure waste; this is the ceiling they were hitting.
    ALL_FIELDS_TIMEOUT = 120.0

    async def list_data_fields_all(self, **params: Any) -> list[BulkField]:
        """Every field in one scope, in one request.

        Needs all four scope parameters and ``version=3.0``.
        The body is taken as bytes and decoded by msgspec: a market is ~40 MB, and the
        standard library's parser spent that on the event loop.
        """
        r = await self.client.request_retrying(
            "GET",
            "/data-fields",
            version=V_FIELDS_ALL,
            params=params,
            raw=True,
            read_timeout=self.ALL_FIELDS_TIMEOUT,
        )
        if not isinstance(r.body, bytes):
            return []
        # Peek at the head instead of decoding twice: an envelope would otherwise only
        # announce itself by failing partway through 40 MB, and stripping the whole body
        # to find its first byte would copy all of it.
        if r.body[:64].lstrip().startswith(b"{"):
            return BULK_FIELDS_ENVELOPE.decode(r.body).results
        return BULK_FIELDS.decode(r.body)

    async def list_data_fields_paged(self, dataset_id: str, **params: Any) -> list[BulkField]:
        """Every field of one dataset in one scope, fifty at a time.

        Region ``ALL`` refuses an unlimited read (``400 ["Invalid query"]``) and caps its flat
        listing well below what it holds, so the only complete route is one dataset at a time.
        Every other region is served whole by :meth:`list_data_fields_all`.
        """
        out: list[BulkField] = []
        offset = 0
        while offset < ALL_FIELDS_CEILING:
            r = await self.client.request_retrying(
                "GET",
                "/data-fields",
                version=V_FIELDS_ALL,
                params={
                    **params,
                    "dataset.id": dataset_id,
                    "limit": ALL_FIELDS_PAGE,
                    "offset": offset,
                },
                raw=True,
            )
            if not isinstance(r.body, bytes):
                break
            page = BULK_FIELDS_ENVELOPE.decode(r.body).results
            out.extend(page)
            if len(page) < ALL_FIELDS_PAGE:
                return out
            offset += ALL_FIELDS_PAGE
        # Still handing out full pages at the ceiling: there is more than can be read this
        # way. Said rather than swallowed — a quietly short catalog is worse than a failed sync.
        raise BrainError(
            f"{dataset_id} has more than {ALL_FIELDS_CEILING:,} fields, which is as far "
            "as BRAIN will page this market."
        )

    async def pyramid_multipliers(self) -> list[dict[str, Any]]:
        """``{category, region, delay, multiplier}`` for every pyramid on this account.

        The response shape is undocumented: ``{pyramids: [...]}``.
        """
        r = await self.client.request_retrying("GET", "/users/self/activities/pyramid-multipliers")
        items = r.body.get("pyramids") if isinstance(r.body, dict) else None
        return items if isinstance(items, list) else []

    async def pyramid_alphas(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """``{category, region, delay, alphaCount}`` submitted between two ISO dates."""
        r = await self.client.request_retrying(
            "GET",
            "/users/self/activities/pyramid-alphas",
            params={"startDate": start_date, "endDate": end_date},
        )
        items = r.body.get("pyramids") if isinstance(r.body, dict) else None
        return items if isinstance(items, list) else []

    #: A region's datasets take eight to sixteen seconds, longer than the client's
    #: ordinary timeout allows for. BRAIN itself gives up at thirty with a 504.
    ALL_SETS_TIMEOUT = 45.0

    async def submission_activity(self) -> list[tuple[str, int]]:
        """Alphas submitted per day, as ``(date, count)`` oldest first.

        BRAIN's own schema titles this "Submitted Alphas". The envelope's ``current`` window
        is two months rather than a quarter, so the dated rows are what a quarterly figure
        has to be built from.
        """
        body = (await self.client.request_retrying("GET", SUBMISSIONS_PATH)).body
        rows = ((body or {}).get("records") or {}).get("records") or []
        return [
            (str(r[0]), int(r[1]))
            for r in rows
            if isinstance(r, list) and len(r) >= 2 and str(r[1]).lstrip("-").isdigit()
        ]

    async def list_data_sets_all(self, **params: Any) -> list[DataSet]:
        """Every dataset matching a scope, however partial that scope is.

        Naming only the region answers for its whole column of markets, which is how a sync
        reads them; all four parameters answer for one market. Omitting ``limit`` returns the
        whole list rather than a page; a ``limit`` above the platform's page cap is refused
        outright. The envelope's ``count`` is checked, so a truncated response fails here
        instead of quietly shrinking the catalog.
        """
        r = await self.client.request_retrying(
            "GET",
            "/data-sets",
            params=params,
            read_timeout=self.ALL_SETS_TIMEOUT,
        )
        body = r.body
        rows = body if isinstance(body, list) else (body or {}).get("results", [])
        if not isinstance(rows, list):
            return []
        total = body.get("count") if isinstance(body, dict) else None
        if isinstance(total, int) and len(rows) < total:
            raise BrainError(f"/data-sets returned {len(rows)} of {total} datasets")
        return [DataSet.model_validate(item) for item in rows]
