"""The BRAIN account: its stored credential, its session, and what the platform allows it.

The account's operators and settings schema are cached here too, because both depend on
the account's permissions.

The session cookie jar is persisted deliberately: signing in costs a proof-of-work solve
and counts against a lockout budget, so a backend restart must not trigger a new one.
"""

import asyncio
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from .brain.auth import Authenticator, SessionInfo
from .brain.endpoints import SIMULATION_TYPES
from .brain.errors import BrainError
from .db.models import BrainSessionRow, Credential, MetadataCache, utcnow
from .sealing import SealError

if TYPE_CHECKING:
    from .brain.endpoints import BrainEndpoints
    from .db.sqlite import Database
    from .sealing import Sealer

log = structlog.get_logger(__name__)

PASSWORD_CONTEXT = "brain-password"  # noqa: S105 - sealing label, not a secret
COOKIE_CONTEXT = "brain-cookies"


class NoCredentialError(RuntimeError):
    """No BRAIN credential has been stored and none was supplied."""


class AuthService:
    """The app's view of "are we signed in to BRAIN?"."""

    def __init__(
        self,
        db: Database,
        sealer: Sealer,
        endpoints: BrainEndpoints,
        *,
        metadata: PlatformMetadata,
    ) -> None:
        self.db = db
        self.sealer = sealer
        self.endpoints = endpoints
        self.metadata = metadata
        self.auth = Authenticator(endpoints)
        self._session = SessionInfo.anonymous()
        self._user_profile: dict[str, Any] | None = None
        #: One session change at a time: a UI sign-in, a silent re-login and a status
        #: refresh would otherwise overwrite each other, leaving the loser's credential current.
        self._lock = asyncio.Lock()
        #: The login typed for a sign-in BRAIN paused for an identity check. Stored once the
        #: check is done (:meth:`verify`), since BRAIN has not accepted it before then.
        self._unverified: tuple[str, str] | None = None

    @property
    def session(self) -> SessionInfo:
        """Last known session state. Cheap; does not hit the network."""
        return self._session

    async def get_user_profile(self) -> dict[str, Any]:
        """Cached user profile from BRAIN /users/{userId}."""
        if not self._session.authenticated or not self._session.user_id:
            return {}
        if self._user_profile is not None:
            return self._user_profile
        try:
            profile = await self.endpoints.get_user(self._session.user_id)
            if profile:
                self._user_profile = profile
                first = str(profile.get("firstName") or "").strip()
                last = str(profile.get("lastName") or "").strip()
                full_name = profile.get("fullName") or (f"{first} {last}".strip() or None)
                if full_name:
                    self._session.full_name = full_name
            return profile
        # The profile is decoration; signing in must not fail over it.
        except Exception as exc:  # noqa: BLE001
            log.warning("brain.user_profile.failed", error=str(exc))
            return {}

    # -- credential storage ----------------------------------------------

    async def store_credential(self, email: str, password: str) -> None:
        """Save (or replace) the BRAIN login, sealed at rest."""
        sealed = self.sealer.seal(password, context=PASSWORD_CONTEXT)
        async with self.db.session() as session:
            existing = await session.scalar(select(Credential).where(Credential.email == email))
            if existing is not None:
                existing.password_sealed = sealed
            else:
                session.add(Credential(email=email, password_sealed=sealed))
        log.info("credential.stored", email=_mask(email))

    async def get_credential(self) -> tuple[str, str] | None:
        """Return the stored ``(email, password)``, unsealed."""
        async with self.db.session() as session:
            credential = await _current_credential(session)
            if credential is None:
                return None
            password = self.sealer.open(credential.password_sealed, context=PASSWORD_CONTEXT)
            return credential.email, password

    async def stored_email(self) -> str | None:
        async with self.db.session() as session:
            credential = await _current_credential(session)
            return credential.email if credential else None

    async def can_sign_in_silently(self) -> bool:
        """Whether there is a stored login the app may use by itself."""
        async with self.db.session() as session:
            credential = await _current_credential(session)
            return credential is not None and credential.silent_off_at is None

    async def _silence(self) -> None:
        """Stop signing in with the stored login by itself, until the person signs in."""
        async with self.db.session() as session:
            credential = await _current_credential(session)
            if credential is not None:
                credential.silent_off_at = utcnow()

    # -- session ---------------------------------------------------------

    async def restore(self) -> SessionInfo:
        """Reuse a cached cookie jar if it is still valid. Called at startup."""
        cookies = await self._load_cookies()
        restored = await self.auth.restore(cookies)
        if restored is None or not restored.authenticated:
            # Unauthenticated but restorable (identity verification pending) keeps its
            # verification link; its cookies are left where they are.
            self._session = restored or SessionInfo.anonymous()
            return self._session
        self._session = restored
        await self._save_cookies()
        await self._warm_operators()
        return restored

    async def login(
        self, email: str | None = None, password: str | None = None, *, silent: bool = False
    ) -> SessionInfo:
        """Sign in, storing the credential if it was supplied here and accepted.

        ``silent`` is the app signing in by itself, which a refused login or Sign out turns
        off (:meth:`can_sign_in_silently`); checked here too, under the lock, so a Sign out
        that lands first wins.
        """
        async with self._lock:
            if silent and not await self.can_sign_in_silently():
                return self._session
            supplied = bool(email and password)
            if not supplied:
                credential = await self.get_credential()
                if credential is None:
                    raise NoCredentialError(
                        "No BRAIN credentials stored. Sign in with your BRAIN email and "
                        "password; they are sealed on this machine and never leave it."
                    )
                email, password = credential
            if email is None or password is None:
                raise NoCredentialError("Enter both your BRAIN email and password.")

            self._user_profile = None
            # A pending verification that was completed finishes the sign-in on its own. One
            # that was not is replaced: pressing Sign in again is how a person asks for a new
            # check, and Persona expires an inquiry ("Session expired") while BRAIN still calls
            # it unfinished, so resuming it handed back the same dead link on every press.
            pending = self._session.inquiry
            info = await self.auth.verify(pending) if pending else None
            if info is None or not info.authenticated:
                info = await self.auth.login(email, password)
            self._session = info
            if info.inquiry and supplied:
                self._unverified = (email, password)
            elif info.refused and not supplied:
                await self._silence()
            if info.authenticated:
                self._unverified = None
                # Stored only once BRAIN accepts it: a mistyped password must not become the
                # credential that silent re-login keeps retrying.
                if supplied:
                    await self.store_credential(email, password)
                # Touched first: _save_cookies files the jar under the most recent sign-in,
                # so a new email's cookies would otherwise land on the previous account's row.
                await self._touch_last_login(email)
                await self._save_cookies()
                # Warm the profile so the first screen can greet them by name without a
                # second round trip. Failure is swallowed inside; a missing name is cosmetic.
                await self.get_user_profile()
                await self._warm_operators()
            return info

    async def verify(self, inquiry: str) -> SessionInfo:
        """Finish a sign-in BRAIN paused for an identity check, without asking again.

        :meth:`login` closes a pending inquiry too, but asks the person to type a password they
        have just typed, and starts a new check when this one is not done. This closes the same
        inquiry on its own, so the check finishing is all it takes.

        Answering before the check is done is the normal case, not a failure: the session
        comes back unauthenticated and still carrying the inquiry, and the caller asks
        again. Nothing here is stored until BRAIN accepts it.
        """
        async with self._lock:
            info = await self.auth.verify(inquiry)
            if info is None:
                # BRAIN would not take the inquiry at all. The session keeps what it had, so
                # the screen still shows the check rather than a bare failure.
                return self._session
            self._user_profile = None
            self._session = info
            if info.authenticated:
                # The login typed for this sign-in, accepted now that the check is done. Filed
                # before the cookies, which are kept under the most recent sign-in's login.
                if self._unverified is not None:
                    email, password = self._unverified
                    self._unverified = None
                    await self.store_credential(email, password)
                    await self._touch_last_login(email)
                await self._save_cookies()
                await self.get_user_profile()
                await self._warm_operators()
            return info

    async def status(self) -> SessionInfo:
        """The session as the platform sees it now; :attr:`session` is the cheap read."""
        # Locked: a check still in flight when a sign-in lands would put the old state back.
        async with self._lock:
            fresh = await self.auth.status()
            # The profile is cached, so re-reading it returns early and would not set the
            # name again on the new object.
            if fresh.authenticated and not fresh.full_name:
                fresh.full_name = self._session.full_name
            if not fresh.authenticated and self._session.inquiry:
                # A refresh that only says "not signed in" must not drop the inquiry the
                # person is part-way through: the next sign-in would mint a replacement and
                # strand the link already open in their browser.
                fresh.inquiry = self._session.inquiry
                fresh.verification_url = self._session.verification_url
                fresh.detail = fresh.detail or self._session.detail
            self._session = fresh
            if self._session.authenticated:
                await self.get_user_profile()
            return self._session

    async def logout(self) -> None:
        async with self._lock:
            self._user_profile = None
            # First, so the stored login stays unused even when BRAIN cannot be told.
            await self._silence()
            await self.endpoints.logout()
            await self._clear_cookies()
            self._session = SessionInfo.anonymous()
            log.info("brain.session.ended")

    async def _warm_operators(self) -> None:
        """Cache the account's own operator list once signed in.

        A failure is logged, never raised: signing in must not fail because Labs and
        validation could not read their operator list.
        """
        try:
            await self.metadata.refresh_operators()
        except Exception:
            log.warning("operators.refresh_failed", exc_info=True)

    # -- cookie persistence ----------------------------------------------

    async def _load_cookies(self) -> list[dict[str, Any]] | None:
        async with self.db.session() as session:
            credential = await _current_credential(session)
            if credential is None:
                return None
            row = await session.scalar(
                select(BrainSessionRow)
                .where(BrainSessionRow.credential_id == credential.id)
                .order_by(BrainSessionRow.updated_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            try:
                return json.loads(self.sealer.open(row.cookies_sealed, context=COOKIE_CONTEXT))
            except SealError, ValueError:
                log.warning("session.cookies_unreadable")
                return None

    async def _save_cookies(self) -> None:
        cookies = self.endpoints.client.export_cookies()
        if not cookies:
            return
        sealed = self.sealer.seal(json.dumps(cookies), context=COOKIE_CONTEXT)

        async with self.db.session() as session:
            credential = await _current_credential(session)
            if credential is None:
                return
            row = await session.scalar(
                select(BrainSessionRow).where(BrainSessionRow.credential_id == credential.id)
            )
            if row is None:
                session.add(BrainSessionRow(credential_id=credential.id, cookies_sealed=sealed))
            else:
                row.cookies_sealed = sealed

    async def _clear_cookies(self) -> None:
        async with self.db.session() as session:
            for row in await session.scalars(select(BrainSessionRow)):
                await session.delete(row)

    async def _touch_last_login(self, email: str) -> None:
        async with self.db.session() as session:
            credential = await session.scalar(select(Credential).where(Credential.email == email))
            if credential is not None:
                credential.last_login_at = utcnow()
                # A sign-in that worked: the app may use this login by itself again.
                credential.silent_off_at = None


#: How long BRAIN's settings schema is trusted before it is read again.
SCHEMA_MAX_AGE = timedelta(hours=1)

#: The cached schema is keyed by what produced it, not just by what it is.
#:
#: It is a *merge* of the per-type trees this build knows about, so a build that merges a
#: different set must not read a row written by one that merged another. Without this, an
#: update that adds a simulation type goes unnoticed for up to :data:`SCHEMA_MAX_AGE` — the
#: new markets simply do not appear, and the release looks broken. Measured on the release
#: that added region-agnostic simulations.
SCHEMA_KEY = f"settings_schema:{'+'.join(SIMULATION_TYPES)}"


class PlatformMetadata:
    """The account's operators and BRAIN's settings schema, cached in SQLite."""

    def __init__(self, db: Database, endpoints: BrainEndpoints) -> None:
        self.db = db
        self.endpoints = endpoints

    async def refresh_metadata(self) -> dict[str, Any]:
        """Cache ``OPTIONS /simulations``: region / universe / neutralization and their
        interdependencies.

        Refreshed rather than hardcoded, because the set changes with new markets and with
        the account's permissions.
        """
        schema = await self.endpoints.settings_schema()
        await self._cache(SCHEMA_KEY, schema)
        return schema

    async def cached_settings_schema(self) -> dict[str, Any] | None:
        """The settings schema, re-read from BRAIN once the stored copy is an hour old.

        Refreshing only at sign-in left a restored session offering universes BRAIN had
        withdrawn. A failed refresh keeps the old copy, still right for almost every setting.
        A build that merges a different set of simulation types finds no copy at all, by
        :data:`SCHEMA_KEY`, and reads a fresh one.
        """
        async with self.db.session() as session:
            row = await session.get(MetadataCache, SCHEMA_KEY)
            cached, fetched = (row.value, row.fetched_at) if row else (None, None)
        if cached is not None and fetched is not None and utcnow() - fetched < SCHEMA_MAX_AGE:
            return cached
        try:
            return await self.refresh_metadata()
        except BrainError:
            log.warning("metadata.schema_refresh_failed", exc_info=True)
            return cached

    async def refresh_operators(self) -> list[dict[str, Any]]:
        operators = await self.endpoints.list_operators()
        payload = [o.model_dump(by_alias=True) for o in operators]
        await self._cache("operators", {"items": payload})
        return payload

    async def cached_operators(self) -> list[dict[str, Any]] | None:
        cached = await self._read_cache("operators")
        return cached.get("items") if cached else None

    async def _cache(self, key: str, value: dict[str, Any]) -> None:
        async with self.db.session() as session:
            row = await session.get(MetadataCache, key)
            if row is None:
                session.add(MetadataCache(key=key, value=value))
            else:
                row.value = value
                row.fetched_at = utcnow()

    async def _read_cache(self, key: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(MetadataCache, key)
            return row.value if row else None


async def _current_credential(session: Any) -> Credential | None:
    """The credential in use: the one that signed in most recently.

    Most-recent rather than oldest-first, so signing in with a different email is the
    account silent re-login and the cookie jar then use.
    """
    return await session.scalar(
        select(Credential)
        .order_by(Credential.last_login_at.desc().nulls_last(), Credential.id.desc())
        .limit(1)
    )


def _mask(email: str) -> str:
    """Never log a full address."""
    name, _, domain = email.partition("@")
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}" if domain else f"{head}***"
