"""Composition root.

One object owns every long-lived resource — both databases, the HTTP client, the
tracker, the sync engine — and wires them together. Routers reach it through a FastAPI
dependency, so nothing constructs its own connections and shutdown is deterministic.
"""

import asyncio
import contextlib
import time
from datetime import timedelta

import structlog
from sqlalchemy import select

from . import preferences, updates
from .account import AuthService, PlatformMetadata
from .brain.client import BrainClient
from .brain.endpoints import BrainEndpoints
from .brain.errors import BrainError, BrainTransportError
from .brain.platform import API_BASE
from .catalog import search
from .catalog.queries import CatalogQueries
from .catalog.sync import CatalogSync, serialise_run
from .config import Settings
from .db.duck import Catalog
from .db.models import SimStatus, SimulationRecord, utcnow
from .db.sqlite import Database, snapshot
from .engine.slots import BatchEngine
from .engine.tracker import SimulationTracker
from .labs.study import Optimizer
from .llm.chat import ChatService
from .llm.registry import ModelRegistry
from .llm.service import LLMService
from .realtime import (
    TOPIC_SESSION,
    TOPIC_SIMULATIONS,
    TOPIC_STUDIES,
    TOPIC_SYNC,
    TOPIC_TASKS,
    Hub,
)
from .sealing import Sealer
from .tasks import TaskRegistry, cancel_background, spawn
from .vault.backfill import Backfill
from .vault.store import AlphaVault

log = structlog.get_logger(__name__)

#: How far back startup looks for finished Alphas the vault missed. Older ones reach it
#: through Sync from BRAIN.
RECAPTURE_WINDOW = timedelta(days=3)

#: Least time between asking BRAIN whether a refused session is usable again.
SESSION_CHECK_SECONDS = 60.0
#: Least time between silent sign-ins; a failed one spends BRAIN's lockout budget.
LOGIN_RETRY_SECONDS = 3600.0


class AppState:
    """Everything the application needs, constructed once."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.settings.ensure_data_dir()

        self.hub = Hub()
        self.tasks = TaskRegistry(
            on_change=lambda payload: self.hub.broadcast(TOPIC_TASKS, payload)
        )
        self.sealer = Sealer(self.settings.key_path)
        self.db = Database(self.settings.sqlite_path)
        self.catalog = Catalog(self.settings.duckdb_path)

        self.client = BrainClient(API_BASE)
        self.endpoints = BrainEndpoints(self.client)
        self.metadata = PlatformMetadata(self.db, self.endpoints)
        self.auth = AuthService(self.db, self.sealer, self.endpoints, metadata=self.metadata)

        self.alphas = AlphaVault(self.catalog)
        self.backfill = Backfill(
            self.alphas,
            self.endpoints,
            self.tasks,
            on_stored=lambda payload: self.hub.broadcast(TOPIC_SIMULATIONS, payload, replay=False),
        )
        self.tracker = SimulationTracker(
            self.db,
            self.endpoints,
            on_change=lambda payload: self.hub.broadcast(TOPIC_SIMULATIONS, payload),
            on_unauthorized=self.renew_session,
            on_alpha=self.backfill.capture,
        )
        self.engine = BatchEngine(
            self.db, self.endpoints, self.tracker, on_unauthorized=self.renew_session
        )
        self.sync = CatalogSync(
            self.db,
            self.catalog,
            self.endpoints,
            tasks=self.tasks,
            on_progress=lambda payload: self.hub.broadcast(TOPIC_SYNC, payload),
        )
        self.queries = CatalogQueries(self.catalog)

        self.models = ModelRegistry(self.db)
        self.llm = LLMService(self.db, self.sealer, self.models)
        self.chat = ChatService(self.db, self.llm, self.queries)
        self.optimizer = Optimizer(
            self.db,
            self.engine,
            self.endpoints,
            self.metadata,
            on_change=lambda payload: self.hub.broadcast(TOPIC_STUDIES, payload),
            alphas=self.alphas,
            backfill=self.backfill,
        )
        self.optimizer.llm = self.llm

        self._renew_lock = asyncio.Lock()
        self._last_session_check = float("-inf")
        self._last_login_attempt = float("-inf")
        #: Set when startup could not read the saved session: BRAIN or the network was down.
        self._restore_pending = False

    # -- lifecycle -------------------------------------------------------

    async def startup(self) -> None:
        # The catalog's single-writer lock is this process's claim on the data directory, so it
        # comes first: a second backend must fail here, before it backs up or migrates.
        await self.catalog.open()
        await asyncio.to_thread(snapshot, self.settings.sqlite_path, updates.current())
        await self.db.create_all()
        preferences.apply(self, await preferences.load(self.db))
        await self.models.load()
        # A catalog downloaded before the search index existed still has none; building it
        # costs a couple of seconds and nothing else depends on it, so it must not block.
        if not await search.ready(self.catalog):
            spawn(search.build(self.catalog), name="catalog-fts-index")
        # Same shape: an Alpha whose series was stored before After-Cost Sharpe existed has none,
        # and it is worked out from that series rather than downloaded again.
        spawn(self.alphas.rebuild_after_cost_sharpe(), name="vault-after-cost-sharpe")

        # Reuse a cached session before anything else; a restart should not cost a
        # proof-of-work solve or count against the sign-in lockout budget.
        try:
            session = await self.auth.restore()
            if session.authenticated:
                log.info("startup.session_restored", user_id=session.user_id)
                # Batching needs MULTI_SIMULATION; without it every batch is one run.
                self.engine.configure_from_permissions(session.permissions)
        except Exception:
            log.warning("startup.session_restore_failed", exc_info=True)
            self._restore_pending = True

        # Not caught: dispatching new work on top of an unreconciled queue is how orphans
        # and double sends start, so a failure here stops startup and says why.
        await self.tracker.reconcile()

        # A sync killed with the process would otherwise report RUNNING forever.
        try:
            await self.sync.reset_interrupted()
            await self._replay_last_sync()
        except Exception:
            log.warning("startup.sync_reset_failed", exc_info=True)

        # A capture in flight when we last stopped died with the process; retry those.
        try:
            await self._recapture()
        except Exception:
            log.warning("startup.recapture_failed", exc_info=True)

        await self.tracker.start()
        await self.engine.start()
        await self.optimizer.start()
        self._session_watch = asyncio.create_task(self._watch_session(), name="session-watch")
        log.info("startup.complete", data_dir=str(self.settings.data_dir))

    async def _replay_last_sync(self) -> None:
        """Seed the sync topic with the last finished run, so its figures survive a restart.

        The hub replays one message per topic to a client on connect, and that snapshot only
        ever held runs this process saw.
        """
        runs = await self.sync.runs(limit=1)
        if runs and runs[0].finished_at is not None:
            await self.hub.broadcast(TOPIC_SYNC, serialise_run(runs[0]))

    async def _recapture(self) -> None:
        """Hand the tracker every recently finished Alpha the vault never stored.

        Captures are fire and forget, held only in memory. The upserts make a repeat harmless.
        """
        since = utcnow() - RECAPTURE_WINDOW
        async with self.db.session() as session:
            ids = (
                await session.scalars(
                    select(SimulationRecord.alpha_id).where(
                        SimulationRecord.status.in_([SimStatus.COMPLETE, SimStatus.WARNING]),
                        SimulationRecord.alpha_id.is_not(None),
                        SimulationRecord.finished_at >= since,
                    )
                )
            ).all()
        wanted = list(dict.fromkeys(str(a) for a in ids if a))
        stored = await self.alphas.by_ids(wanted)
        missing = [a for a in wanted if a not in stored]
        if missing:
            log.warning("startup.recapture", count=len(missing))
        for alpha_id in missing:
            self.tracker.alpha_landed(alpha_id)

    async def _watch_session(self) -> None:
        """Renew the BRAIN session shortly before it expires, so running work keeps going, and
        read the saved one again while startup could not."""
        while True:
            await asyncio.sleep(60)
            try:
                session = self.auth.session
                left = session.expires_in_seconds
                if session.authenticated:
                    self._restore_pending = False
                    if left is not None and left <= 600:
                        await self.renew_session(expiring=True)
                elif self._restore_pending and not session.inquiry:
                    restored = await self.auth.restore()
                    self._restore_pending = False
                    if restored.authenticated:
                        self.engine.configure_from_permissions(restored.permissions)
                        await self.hub.broadcast(TOPIC_SESSION, restored.to_dict())
            except BrainError as exc:
                # BRAIN or the network still down: the next minute tries again.
                log.info("session.watch_unreachable", error=str(exc))
            # One failed check must not end renewal for the rest of the process.
            except Exception:
                log.exception("session.watch_failed")

    async def renew_session(self, *, expiring: bool = False) -> bool:
        """Whether BRAIN will take requests now, signing in again if it will not.

        Called on every 401, so it is guarded four ways: one renewal at a time, the
        platform asked at most once a minute, one silent sign-in per hour, and none at all
        once BRAIN refused the stored login or the person signed out — each failed sign-in
        spends BRAIN's lockout budget.
        """
        async with self._renew_lock:
            now = time.monotonic()
            if not expiring:
                if now - self._last_session_check < SESSION_CHECK_SECONDS:
                    return False
                self._last_session_check = now
                try:
                    if (await self.auth.status()).authenticated:
                        return True
                except Exception:
                    log.warning("session.check_failed", exc_info=True)
                    return False
            if now - self._last_login_attempt < LOGIN_RETRY_SECONDS:
                return False
            if self.auth.session.inquiry:
                # An identity check only the person can finish. A silent sign-in would start
                # yet another, and each spends BRAIN's lockout budget: hourly, that locked an
                # account out without anyone signing in.
                return False
            if not await self.auth.can_sign_in_silently():
                return False
            # Set only for a sign-in BRAIN may have answered: the hour exists to protect the
            # lockout budget, and a request that never arrived spent none of it.
            previous, self._last_login_attempt = self._last_login_attempt, now
            try:
                info = await self.auth.login(silent=True)
                if info.authenticated:
                    # A sign-in that worked spent none of the lockout budget.
                    self._last_login_attempt = float("-inf")
                    self.engine.configure_from_permissions(info.permissions)
                await self.hub.broadcast(TOPIC_SESSION, info.to_dict())
                return info.authenticated
            except BrainTransportError as exc:
                if not exc.maybe_delivered:
                    self._last_login_attempt = previous
                log.warning("session.renew_unreachable", exc_info=True)
                return False
            except Exception:
                log.warning("session.renew_failed", exc_info=True)
                return False

    async def shutdown(self) -> None:
        watch = getattr(self, "_session_watch", None)
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        await self.optimizer.stop()
        await self.engine.stop()
        await self.tracker.stop()
        # Detached work reads and writes the stores closed below, so it goes first.
        await cancel_background()
        await self.backfill.stop()
        await self.sync.shutdown()
        await self.client.aclose()
        await self.llm.aclose()
        await self.catalog.close()
        await self.db.dispose()
        log.info("shutdown.complete")
