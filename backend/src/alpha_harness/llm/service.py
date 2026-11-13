"""Calling the model.

Every call goes through :meth:`LLMService.generate`, which does the same four things in
the same order: pick a key with budget, send, record what it actually cost, and — when
Google disagrees with our accounting — mark that pair spent and try the next key rather
than retrying into the same wall.

Rotation chooses by remaining daily budget and believes a ``429`` immediately, because a
retry loop that tries each key in turn burns a request from every key on a bad day.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
import structlog
from openai import DefaultAsyncHttpxClient

from ..schemas import camel_dict
from .budget import Ledger
from .keys import BudgetExhaustedError, KeyStore, LLMError, LLMUnavailable
from .openai_compat import OpenAICompatible, ProviderError
from .providers import PROVIDERS
from .providers import get as get_provider
from .text import estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai.types.chat.completion_create_params import ResponseFormat
    from openai.types.shared import ReasoningEffort

    from ..db.sqlite import Database
    from ..sealing import Sealer
    from .registry import ModelInfo, ModelRegistry

log = structlog.get_logger(__name__)

#: How long a single generation may take before we give up on it.
TIMEOUT_SECONDS = 180.0


@dataclass(slots=True)
class Answer:
    """One model response, and the tokens it cost."""

    text: str
    model: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int

    @property
    def usage(self) -> dict[str, int]:
        return camel_dict(self, exclude=("text", "model"))


def _status(exc: Exception) -> int | None:
    return exc.status if isinstance(exc, ProviderError) else None


def _is_rate_limit(exc: Exception) -> tuple[bool, bool]:
    """``(rate limited, daily)``. Only the provider's details say which limit was hit."""
    text = str(exc).lower()
    if _out_of_credit(text):
        return False, False
    limited = _status(exc) == 429 or "resource_exhausted" in text or "quota" in text
    daily = (
        "per day" in text
        or "perday" in text
        or "daily" in text
        or "requests per day" in text
        or _no_quota(text)
    )
    return limited, daily


def _no_quota(text: str) -> bool:
    """Google says ``limit: 0`` when a model has no free quota at all on that key."""
    return re.search(r"limit:\s*0(?![\d.])|quotavalue\W+0(?![\d.])", text) is not None


def _rate_limit_reason(exc: Exception, model_id: str, *, daily: bool) -> str:
    """What to show against a key the provider turned away, instead of its JSON."""
    if _no_quota(str(exc).lower()):
        return f"{model_id} has no free quota on this key. Choose another model."
    if daily:
        return f"{model_id} has used its daily allowance on this key."
    return f"{model_id} is over its per-minute limit on this key."


def _plain_reason(exc: Exception) -> str | None:
    """The common failures, said in words the person reading them can act on.

    Google's errors arrive as a wall of JSON; the raw text is still kept against the key as
    ``lastError`` for anyone who wants it.
    """
    text, status = str(exc).lower(), _status(exc)
    if "api_key_invalid" in text or "api key not valid" in text or "unauthenticated" in text:
        return (
            "That Google AI Studio key was not accepted. Check it was copied whole, and "
            "that it has not been deleted at aistudio.google.com."
        )
    if status == 403 or "permission_denied" in text:
        return (
            "The provider refused that key. It may not have access to this model, or the "
            "account or project it belongs to may have been closed."
        )
    if status == 404 or ("not found" in text and "model" in text):
        return "That model is not available to this key. Pick a different one."
    if status == 503:
        return (
            "That model is overloaded right now, which usually passes within a minute. "
            "Try again, or pick another model."
        )
    return None


def _rejects_temperature(exc: Exception) -> bool:
    """A 400 about the temperature: the model takes none but its own default."""
    return _status(exc) == 400 and "temperature" in str(exc).lower()


#: How providers word a refusal of the answer's format rather than of the request.
_FORMAT_WORDS = ("response_format", "response format", "schema", "json_object", "json mode")


def _rejects_format(exc: Exception) -> bool:
    """A 400 about the answer's format: the model takes a looser one. Servers that validate
    with FastAPI or TGI answer 422 for the same thing."""
    text = str(exc).lower()
    return _status(exc) in (400, 422) and any(word in text for word in _FORMAT_WORDS)


def _refused(exc: Exception) -> bool:
    """Whether the key was turned away rather than the request: another key may get in."""
    text = str(exc).lower()
    return _status(exc) in (401, 403) or "api_key_invalid" in text or _out_of_credit(text)


def _out_of_credit(text: str) -> bool:
    """An account with no credit left: a ``429`` naming a quota, but no wait ends it."""
    return "insufficient_quota" in text


#: How long a connection to a provider stays open between calls: a fresh one costs about
#: 0.4 s of set-up, measured against Google, on answers that take two or three seconds.
KEEPALIVE_SECONDS = 60.0

#: Models that reject ``minimal``: "not supported for Gemini 3.8 Flash" per Google's docs.
_NO_MINIMAL = ("gemini-3.7", "gemini-3.8")

_EFFORTS: dict[str, ReasoningEffort] = {
    "MINIMAL": "minimal",
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
}


def _effort(model: ModelInfo, thinking: str | None) -> ReasoningEffort:
    """Callers ask for a thinking level; this is the ``reasoning_effort`` the model accepts.

    Only Gemini 3 takes one, and 3.7 and 3.8 refuse ``minimal`` with a 400 on every message.
    """
    if not thinking or model.provider != "google" or not model.id.startswith("gemini-3"):
        return None
    effort = _EFFORTS[thinking.upper()]
    return "low" if effort == "minimal" and model.id.startswith(_NO_MINIMAL) else effort


#: Providers that take a ``prompt_cache_key``, which keeps requests sharing one on the same
#: prompt cache: Mistral says it raises the chance of a hit, OpenAI that it steers routing.
_CACHE_KEYS = frozenset({"mistral", "openai"})

#: Providers whose strict schemas take an array's length. Anthropic refuses ``maxItems`` and a
#: ``minItems`` above 1 with a 400, Cerebras both, and the rest don't document either.
_ARRAY_LENGTHS = frozenset({"google", "openai"})


def _without_lengths(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _without_lengths(v) for k, v in node.items() if k not in {"minItems", "maxItems"}
        }
    if isinstance(node, list):
        return [_without_lengths(v) for v in node]
    return node


def _formats(provider: str, schema: dict[str, Any]) -> tuple[ResponseFormat | None, ...]:
    """What a model can be asked to answer in, strictest first: the schema, which the provider
    holds the answer to; any JSON object; or the prompt's own words alone. Every prompt names
    its keys and every parser reads a fenced block too, so each one still works."""
    if provider not in _ARRAY_LENGTHS:
        schema = _without_lengths(schema)
    return (
        {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema, "strict": True},
        },
        {"type": "json_object"},
        None,
    )


class LLMService:
    """The assistant: keys, budget and models in one place."""

    def __init__(self, db: Database, sealer: Sealer, registry: ModelRegistry) -> None:
        self.db = db
        self.registry = registry
        self.ledger = Ledger(db)
        self.keys = KeyStore(db, sealer, self.ledger)
        #: Models that refused a temperature. Learned from the refusal, because nothing
        #: publishes which models take one; forgotten on restart, costing one 400 each.
        self._default_sampling: set[str] = set()
        #: How many steps down :func:`_formats` each model has gone, learned the same way:
        #: not every model takes a schema, and Anthropic takes nothing else.
        self._looser: dict[str, int] = {}
        self._http = DefaultAsyncHttpxClient(
            timeout=TIMEOUT_SECONDS, limits=httpx2.Limits(keepalive_expiry=KEEPALIVE_SECONDS)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing --------------------------------------------------------

    def _client(self, provider: str, secret: str) -> OpenAICompatible:
        # PROVIDERS itself, not get(): a retired provider has a stub there with no endpoint.
        spec = PROVIDERS.get(provider)
        if spec is None:
            gone = get_provider(provider)
            raise LLMError(f"{gone.label} cannot be used. {gone.retired}")
        # Anthropic's native endpoints (the model list the key check reads) want these two
        # headers and answer "x-api-key header is required" to Bearer alone.
        headers = (
            {"x-api-key": secret, "anthropic-version": "2023-06-01"}
            if provider == "anthropic"
            else None
        )
        return OpenAICompatible(spec.base_url, secret, self._http, headers)

    async def runnable(self) -> list[dict[str, Any]]:
        """Set-up models whose provider has an enabled Key, most requests left today first.

        A model set up without such a Key is listed under LLM Integration but cannot answer,
        so nothing that sends a request offers it.
        """
        status = await self.keys.status(self.registry)
        enabled = {k["provider"] for k in status["keys"] if k["enabled"]}
        found = [
            {
                "id": b["model"],
                "ref": b["ref"],
                "provider": b["provider"],
                "remainingToday": b["remainingToday"],
            }
            for b in status["budget"]
            if b["provider"] in enabled
        ]
        return sorted(found, key=lambda m: -m["remainingToday"])

    def model_for(self, ref: str | None) -> ModelInfo:
        """The model asked for, or with none named, the set-up one with most to spend."""
        if not ref:
            models = [m for m in self.registry.all() if m.provider in PROVIDERS]
            if not models:
                raise LLMError("No model is set up yet. Set one up under LLM Integration › Models.")
            return models[0]
        model = self.registry.get(ref)
        if model is None:
            raise LLMError(f"{ref} is not set up. Set it up under LLM Integration › Models.")
        if model.retired:
            raise LLMError(
                f"{model.id} is not used: {model.retired} Remove it under LLM Integration › "
                "Models and pick another model."
            )
        return model

    # -- the one call ----------------------------------------------------

    async def generate(
        self,
        *,
        system: str,
        user: str,
        model_ref: str | None,
        response_schema: dict[str, Any],
        temperature: float,
        thinking: str | None = None,
    ) -> Answer:
        """Send one prompt, rotating keys by remaining budget. ``model_ref`` names the model
        as :func:`.registry.model_ref` spells it.

        ``thinking`` is one of Google's thinking levels. Not a free upgrade: thinking tokens
        are billed against the same per-minute budget as the answer.

        ``response_schema`` goes as a strict JSON schema; a model that refuses it is asked for
        a looser format from then on, as :func:`_formats` lists them.
        """
        model = self.model_for(model_ref)
        estimate = estimate_tokens(system) + estimate_tokens(user)
        tried: set[int] = set()
        refusal: LLMError | None = None
        effort = _effort(model, thinking)
        formats = _formats(model.provider, response_schema)
        # One per system prompt: every call of a lab, or of the assistant, starts the same way.
        cache_key = (
            hashlib.sha256(system.encode()).hexdigest()[:16]
            if model.provider in _CACHE_KEYS
            else None
        )
        sampling = None if model.ref in self._default_sampling else temperature

        while True:
            level = self._looser.get(model.ref, 0)
            try:
                key_id = await self.keys.choose(model, skip=tried)
            except BudgetExhaustedError as exc:
                # Every key has been tried: one that was turned away says more than a budget.
                if refusal is not None:
                    raise refusal from None
                if tried:
                    exc.args = (f"{exc.args[0]} Already tried: {len(tried)} key(s).",)
                raise
            tried.add(key_id)

            # Settled however this ends: answered and recorded, failed, or retried.
            try:
                secret = await self.keys.secret(key_id)
                client = self._client(model.provider, secret)

                try:
                    text, usage = await asyncio.wait_for(
                        client.generate(
                            model=model.id,
                            system=system,
                            user=user,
                            temperature=sampling,
                            response_format=formats[level],
                            reasoning_effort=effort,
                            prompt_cache_key=cache_key,
                        ),
                        timeout=TIMEOUT_SECONDS,
                    )
                except TimeoutError as exc:
                    await self.keys.mark(key_id, error="Timed out")
                    # The provider had the request and may well bill it, so it counts.
                    await self.ledger.record(key_id, model, estimate)
                    raise LLMUnavailable(
                        f"{model.id} did not answer within {TIMEOUT_SECONDS:.0f} seconds. "
                        "Try a smaller question or a lighter model."
                    ) from exc
                except Exception as exc:
                    if sampling is not None and _rejects_temperature(exc):
                        # The key is fine; the model only wants its own default. Same key again.
                        log.info("llm.temperature_refused", model=model.id)
                        self._default_sampling.add(model.ref)
                        sampling = None
                        tried.discard(key_id)
                        continue
                    if level + 1 < len(formats) and _rejects_format(exc):
                        # Likewise for the format: the next one down, on the same key.
                        log.info("llm.format_refused", model=model.id, error=str(exc)[:200])
                        self._looser[model.ref] = max(level + 1, self._looser.get(model.ref, 0))
                        tried.discard(key_id)
                        continue
                    limited, daily = _is_rate_limit(exc)
                    await self.keys.mark(
                        key_id,
                        error=_rate_limit_reason(exc, model.id, daily=daily)
                        if limited
                        else str(exc)[:300],
                    )
                    if limited:
                        # Google is the authority: mark this pair spent and rotate rather than
                        # retrying into a limit we evidently mis-tracked.
                        await self.ledger.penalise(key_id, model, daily=daily)
                        log.warning("llm.rate_limited", key_id=key_id, model=model.id, daily=daily)
                        continue
                    # 5xx is the provider's own trouble ("overloaded"): another model may answer.
                    kind = LLMUnavailable if (_status(exc) or 0) >= 500 else LLMError
                    error = kind(_plain_reason(exc) or f"{model.id} could not be reached: {exc}")
                    if _refused(exc):
                        log.warning("llm.key_refused", key_id=key_id, model=model.id)
                        refusal = error
                        continue
                    raise error from exc

                prompt = int(usage.get("prompt_tokens") or 0)
                output = int(usage.get("completion_tokens") or 0)
                reported = int(usage.get("total_tokens") or 0)
                # Google reports no breakdown: its thinking is only what the total holds beyond
                # the prompt and the answer.
                thought = int(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                ) or max(0, reported - prompt - output)
                total = reported or estimate
                await self.ledger.record(key_id, model, total)
                await self.keys.mark(key_id)

                return Answer(
                    text=text.strip(),
                    model=model.id,
                    prompt_tokens=prompt,
                    output_tokens=output,
                    thinking_tokens=thought,
                    total_tokens=total,
                )
            finally:
                self.ledger.settled(key_id, model)

    # -- health ----------------------------------------------------------

    async def _listed(self, provider: str, key_id: int) -> list[str]:
        secret = await self.keys.secret(key_id)
        # Anthropic pages its list twenty at a time; Google refuses unknown parameters.
        query = {"limit": 1000} if provider == "anthropic" else {}
        return await self._client(provider, secret).models(**query)

    async def check_key(self, key_id: int) -> dict[str, Any]:
        """Confirm a key works, without spending a generation request.

        ``models.list`` is not billed against the generation quotas, so a health check
        costs nothing that matters.
        """
        row = await self.keys.get(key_id)
        provider = str(getattr(row, "provider", "google") or "google")
        try:
            names = await self._listed(provider, key_id)
        # Any provider failure is the key test's answer, stored and shown to the user.
        except Exception as exc:  # noqa: BLE001
            await self.keys.mark(key_id, error=str(exc)[:300])
            return {"keyId": key_id, "ok": False, "error": str(exc)[:300]}

        await self.keys.mark(key_id)
        return {"keyId": key_id, "ok": True, "models": len(names)}

    async def offered(self, provider: str) -> dict[str, Any]:
        """The model ids a provider's keys can reach, to pick from when setting one up.

        Asked of the first enabled key, live: the list is only a convenience, so a failure
        says why and leaves the user to type the id instead.
        """
        rows = [r for r in await self.keys.list_keys() if r.enabled and r.provider == provider]
        if not rows:
            return {"models": [], "error": "Add a Key for this provider first."}
        try:
            return {"models": await self._listed(provider, rows[0].id), "error": None}
        # The reason is shown beside the field where the id can still be typed.
        except Exception as exc:  # noqa: BLE001
            return {"models": [], "error": str(exc)[:300]}


if __name__ == "__main__":
    import json
    import sys
    from types import SimpleNamespace

    from ..labs.power_pool import SCHEMA as ALPHAS
    from ..labs.power_pool import parse_alphas
    from ..tools import description
    from .chat import FIELD_PICK_SCHEMA
    from .chat import _parse as read_picks  # pyright: ignore[reportPrivateUsage]
    from .registry import ModelInfo as Model

    def closed(node: Any) -> bool:
        """Every object shut to other keys and every key required, as strict schemas want."""
        if isinstance(node, list):
            return all(closed(v) for v in node)
        if not isinstance(node, dict):
            return True
        shut = node.get("type") != "object" or (
            node.get("additionalProperties") is False
            and set(node.get("required", [])) == set(node.get("properties", {}))
        )
        return shut and all(closed(v) for v in node.values())

    def completion(content: str) -> httpx2.Response:
        message = {"role": "assistant", "content": content}
        body: dict[str, Any] = {"id": "c", "object": "chat.completion", "created": 0, "model": "m"}
        body["choices"] = [{"index": 0, "finish_reason": "stop", "message": message}]
        body["usage"] = {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
        return httpx2.Response(200, json=body)

    def refusal(message: str, status: int = 400) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": message}})

    def kind(body: dict[str, Any]) -> str | None:
        return (body.get("response_format") or {}).get("type")

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    opened: list[LLMService] = []

    def service(
        provider: str, answer: Callable[[dict[str, Any]], httpx2.Response]
    ) -> tuple[Any, list[dict[str, Any]]]:
        """An LLMService with one key, whose provider answers each request with ``answer``."""
        sent: list[dict[str, Any]] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return answer(sent[-1])

        model = Model(id="m", provider=provider, rpm=60, rpd=1000, reset_timezone="UTC")

        async def choose(info: Model, *, skip: set[int]) -> int:
            if 1 in skip:
                raise BudgetExhaustedError(info, [])
            return 1

        async def secret(_key_id: int) -> str:
            return "test-key"

        llm: Any = LLMService.__new__(LLMService)
        llm.registry = SimpleNamespace(get=lambda _ref: model)
        llm.keys = SimpleNamespace(choose=choose, secret=secret, mark=nothing)
        llm.ledger = SimpleNamespace(record=nothing, penalise=nothing, settled=lambda *_: None)
        llm._default_sampling, llm._looser = set[str](), dict[str, int]()
        llm._http = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle))
        opened.append(llm)
        return llm, sent

    async def ask(llm: Any, schema: dict[str, Any]) -> Answer:
        return await llm.generate(
            system="s", user="u", model_ref="m", response_schema=schema, temperature=0.5
        )

    alphas = json.dumps(
        {"alphas": [{"expression": f"rank(ts_delta(close, {d}))"} for d in range(20)]}
    )
    parts = json.dumps({part: f"{part} " * 30 for part in description.PARTS})

    async def mocked() -> dict[str, bool]:
        found: dict[str, bool] = {}

        llm, sent = service("google", lambda _body: completion(alphas))
        answer = await ask(llm, ALPHAS)
        schema = sent[0]["response_format"]["json_schema"]
        found["google: strict schema, lengths kept"] = (
            schema["strict"] is True and schema["schema"] == ALPHAS
        )
        found["a schema answer parses as before"] = len(parse_alphas(answer.text)) == 20
        found["no cache key where none is taken"] = "prompt_cache_key" not in sent[0]

        llm, sent = service("mistral", lambda _body: completion(alphas))
        await ask(llm, ALPHAS)
        await ask(llm, ALPHAS)
        keys = {b.get("prompt_cache_key") for b in sent}
        found["mistral: one cache key for one system prompt"] = len(keys) == 1 and None not in keys

        llm, sent = service(
            "groq",
            lambda body: (
                refusal("`json_schema` is not supported with this model")
                if kind(body) == "json_schema"
                else completion(alphas)
            ),
        )
        first = await ask(llm, ALPHAS)
        await ask(llm, ALPHAS)
        found["400 to json_schema: json_object on the same key, remembered"] = [
            kind(b) for b in sent
        ] == ["json_schema", "json_object", "json_object"] and len(parse_alphas(first.text)) == 20
        found["no array lengths where they are not documented"] = "minItems" not in json.dumps(
            sent[0]["response_format"]
        )

        llm, sent = service(
            "anthropic",
            lambda body: (
                completion(parts)
                if kind(body) is None
                else refusal(f"response_format: {kind(body)} is not supported")
            ),
        )
        written = description.read((await ask(llm, description.SCHEMA)).text)
        found["refused twice: the prompt alone, still read"] = (
            [kind(b) for b in sent] == ["json_schema", "json_object", None]
            and "response_format" not in sent[2]
            and written is not None
        )

        llm, sent = service(
            "huggingface",
            lambda body: (
                refusal("Input validation error: grammar schema", 422)
                if kind(body) == "json_schema"
                else completion(parts)
            ),
        )
        await ask(llm, description.SCHEMA)
        found["422 about the schema steps down too"] = [kind(b) for b in sent] == [
            "json_schema",
            "json_object",
        ]

        llm, sent = service("openai", lambda _body: refusal("maximum context length is 8192"))
        try:
            await ask(llm, ALPHAS)
            found["any other 400 fails as before"] = False
        except LLMError:
            found["any other 400 fails as before"] = len(sent) == 1 and not llm._looser

        for each in opened:
            await each.aclose()
        return found

    nothing_fits = {"reply": "Nothing fits.", "picks": [], "datasets": []}
    checks = {
        "every response schema is strict": all(
            closed(s) for s in (ALPHAS, description.SCHEMA, FIELD_PICK_SCHEMA)
        ),
        "lengths only for google and openai, schema untouched": (
            "minItems" in json.dumps(_formats("openai", ALPHAS)[0])
            and "minItems" not in json.dumps(_formats("cerebras", ALPHAS)[0])
            and ALPHAS["properties"]["alphas"]["minItems"] == 20
        ),
        "picks read as before": read_picks(json.dumps(nothing_fits)) == nothing_fits,
        **asyncio.run(mocked()),
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"service self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
