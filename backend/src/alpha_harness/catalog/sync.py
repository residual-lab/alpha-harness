"""Download the BRAIN data catalog into DuckDB.

Scope is one ``(instrumentType, region, delay, universe)`` tuple per run, because that is
exactly how the platform scopes ``/data-fields`` — a field that exists in USA/delay-1
may simply not exist in EUR/delay-0. Storing one row per field *per tuple* is what makes
"which fields are in both delays" a single query later.

``GET /data-fields`` at ``version=3.0`` with all four scope parameters returns a whole
scope in one response. Datasets are paged. Categories need no read of their own: every field
names its category and subcategory.
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update

from ..brain.errors import BrainError
from ..brain.platform import INSTRUMENT_TYPE, REGION_AGNOSTIC_REGION
from ..brain.schemas import BulkField, FieldRef
from ..db.duck import DATASET_COLUMNS, CatalogUnusableError
from ..db.models import SyncRun, SyncStatus, utcnow
from . import search
from .queries import scope_label

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from ..brain.endpoints import BrainEndpoints
    from ..brain.schemas import DataSet
    from ..db.duck import Catalog
    from ..db.sqlite import Database
    from ..tasks import Task, TaskRegistry

log = structlog.get_logger(__name__)

#: Scopes whose fields are held in memory and downloading at once in a full sync. BRAIN
#: throttles to roughly 34 requests a minute serially but 67 at eight in flight, so this is
#: what the platform allows rather than what the local machine could manage.
ALL_CONCURRENCY = 8

#: How long a market waits for the shared dataset read before fetching its own. Short: a
#: market that waits is a market not making progress, and its own request costs ~2s.
BULK_DATASETS_WAIT = 12.0

#: Regions read at once for the shared dataset index. BRAIN allows a request a second, so a
#: wider fan-out just trades 429s for the latency it saves.
REGION_DATASETS_CONCURRENCY = 4

#: Datasets paged at once when a market has to be read dataset by dataset (region ALL).
#: 139 datasets and ~640 requests: serial is eleven minutes, and any wider draws 429s.
PAGED_FIELDS_CONCURRENCY = 4

#: Tries per market for each stage of a full sync. The client already retries throttling
#: and server errors per request; this also covers an empty answer or a failed write.
SCOPE_ATTEMPTS = 3

#: Progress steps per market state in a full sync: its fields arrive (1), its details arrive (2).
#: A market that failed, or kept only its fields, is finished.
_STEPS = {"waiting": 0, "fetching": 0, "details": 1, "fields": 2, "done": 2, "failed": 2}

ALL_LABEL = "All BRAIN datasets"


class SyncCancelled(RuntimeError):
    """The user stopped a running sync."""


@dataclass(frozen=True, slots=True)
class SyncTarget:
    """The scope of one download."""

    instrument_type: str
    region: str
    delay: int
    universe: str

    @property
    def params(self) -> dict[str, Any]:
        return {
            "instrumentType": self.instrument_type,
            "region": self.region,
            "delay": self.delay,
            "universe": self.universe,
        }

    @property
    def label(self) -> str:
        return scope_label(self.instrument_type, self.region, self.delay, self.universe)


#: The run row that stands for a whole-catalog sync rather than one scope.
ALL_TARGET = SyncTarget(INSTRUMENT_TYPE, "*", -1, "*")


class CatalogSync:
    """Runs and reports catalog downloads."""

    def __init__(
        self,
        db: Database,
        catalog: Catalog,
        endpoints: BrainEndpoints,
        *,
        tasks: TaskRegistry,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.endpoints = endpoints
        self.tasks = tasks
        self._on_progress = on_progress
        #: The background-task row a full sync reports through, so it shows up alongside
        #: every other long job rather than only on the screen that started it.
        self._task_by_run: dict[int, Task] = {}
        self._cancels: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._targets: dict[int, SyncTarget] = {}
        #: Live stage and scope counts of a running full sync, by run id.
        self._all: dict[int, dict[str, Any]] = {}

    # -- public API ------------------------------------------------------

    async def start_all(self, targets: list[SyncTarget]) -> dict[str, Any]:
        """Download every scope in the background and return its progress immediately.

        One run row stands for the whole download, so there is one progress feed and one
        cancel. Every market runs its own pipeline at once: its fields first (with dataset
        rows derived from them, so it is browsable immediately), then BRAIN's full dataset
        records. A market that fails is noted and skipped rather than ending the sync.
        """
        for run_id in list(self._all):
            if run_id in self._tasks:
                live = await self.get_run(run_id)
                if live is not None:
                    return self._payload(live)

        run = await self._open_run(ALL_TARGET)
        cancel = asyncio.Event()
        self._cancels[run.id] = cancel
        self._targets[run.id] = ALL_TARGET
        self._all[run.id] = {
            "failed": [],
            # Per market, for the sync matrix: waiting, fetching, fields, details, done, failed.
            "markets": {
                t.label: {
                    "region": t.region,
                    "delay": t.delay,
                    "universe": t.universe,
                    "state": "waiting",
                    "fields": None,
                }
                for t in targets
            },
        }

        self._task_by_run[run.id] = await self.tasks.start(
            "catalog-sync", "Downloading BRAIN Data Fields", runId=run.id
        )
        task = asyncio.create_task(
            self._guarded(run.id, ALL_LABEL, self._crawl_all(run.id, targets, cancel)),
            name=f"sync-all-{run.id}",
        )
        self._tasks[run.id] = task

        def _done(_t: asyncio.Task[None], rid: int = run.id) -> None:
            self._tasks.pop(rid, None)
            self._targets.pop(rid, None)
            self._all.pop(rid, None)

        task.add_done_callback(_done)
        return self._payload(run)

    async def reset_interrupted(self) -> int:
        """Mark runs left RUNNING by a crash as FAILED, so they stop reading as in progress.

        Called at startup, before anything can be running in this process.
        """
        async with self.db.session() as session:
            result = await session.execute(
                update(SyncRun)
                .where(SyncRun.status == SyncStatus.RUNNING)
                .values(
                    status=SyncStatus.FAILED,
                    error="The backend stopped during this sync.",
                    finished_at=utcnow(),
                )
            )
            return int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]  # DML returns a CursorResult; stubs say Result

    async def cancel(self, run_id: int) -> bool:
        event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    async def get_run(self, run_id: int) -> SyncRun | None:
        async with self.db.session() as session:
            return await session.get(SyncRun, run_id)

    async def runs(self, limit: int = 50) -> list[SyncRun]:
        async with self.db.session() as session:
            result = await session.execute(
                select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def shutdown(self) -> None:
        for event in self._cancels.values():
            event.set()
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- the download ----------------------------------------------------

    async def _guarded(self, run_id: int, label: str, work: Awaitable[None]) -> None:
        state, error = "done", None
        try:
            await work
        except SyncCancelled:
            state = "cancelled"
            await self._finish(run_id, SyncStatus.CANCELLED)
            log.info("sync.cancelled", run_id=run_id, target=label)
        except asyncio.CancelledError:
            state, error = "cancelled", "Backend shut down"
            await self._finish(run_id, SyncStatus.CANCELLED, error=error)
            raise
        except BrainError as exc:
            state, error = "failed", exc.message
            await self._finish(run_id, SyncStatus.FAILED, error=error)
            log.warning("sync.failed", run_id=run_id, error=error)
        except Exception as exc:
            state, error = "failed", str(exc)
            await self._finish(run_id, SyncStatus.FAILED, error=error)
            log.exception("sync.crashed", run_id=run_id)
        finally:
            self._cancels.pop(run_id, None)
            if (row := self._task_by_run.pop(run_id, None)) is not None:
                await self.tasks.finish(row, state=state, error=error)

    async def _all_datasets(
        self, targets: list[SyncTarget]
    ) -> dict[tuple[str, int, str], list[DataSet]] | None:
        """Every market's datasets, read a region at a time, keyed by region, delay and universe.

        One unscoped read covers all of them on paper, but BRAIN answers it with a 504 at its
        thirty-second gateway limit; a single region comes back in eight to sixteen seconds.
        Regions are read together, and one that fails sends only its own markets to their own
        requests.
        """
        scopes = sorted({(t.instrument_type, t.region) for t in targets})
        gate = asyncio.Semaphore(REGION_DATASETS_CONCURRENCY)

        async def one(instrument_type: str, region: str) -> list[DataSet]:
            async with gate:
                return await self.endpoints.list_data_sets_all(
                    instrumentType=instrument_type, region=region
                )

        results = await asyncio.gather(*(one(i, r) for i, r in scopes), return_exceptions=True)
        indexed: dict[tuple[str, int, str], list[DataSet]] = {}
        failed = 0
        for (_, region), result in zip(scopes, results, strict=True):
            if isinstance(result, BaseException):
                failed += 1
                log.warning("sync_all.region_datasets_failed", region=region, error=_reason(result))
                continue
            for row in result:
                if row.region is None or row.delay is None or row.universe is None:
                    continue
                indexed.setdefault((row.region, row.delay, row.universe), []).append(row)
        if failed == len(scopes):
            return None
        log.info("sync_all.bulk_datasets", regions=len(scopes) - failed, markets=len(indexed))
        return indexed

    async def _fields(
        self,
        target: SyncTarget,
        bulk_datasets: asyncio.Task[dict[tuple[str, int, str], list[DataSet]] | None],
        market: dict[str, Any],
        run_id: int,
        cancel: asyncio.Event,
    ) -> list[BulkField]:
        """One market's fields, however the platform is willing to hand them over."""
        if target.region != REGION_AGNOSTIC_REGION:
            return await self.endpoints.list_data_fields_all(**target.params)

        # Region ALL refuses an unlimited read and stops answering past 10,000 of the ~28,000
        # fields it holds, so it is read one dataset at a time. Hundreds of requests, hence
        # the running count: a market that reports nothing for ten minutes looks hung.
        datasets = await _shared_datasets(bulk_datasets, target)
        if datasets is None:
            datasets = await self.endpoints.list_data_sets_all(**target.params)
        gate = asyncio.Semaphore(PAGED_FIELDS_CONCURRENCY)
        out: list[BulkField] = []
        lost: list[str] = []

        async def one(dataset_id: str) -> None:
            async with gate:
                _check(cancel)
                try:
                    found = await self.endpoints.list_data_fields_paged(dataset_id, **target.params)
                # One dataset BRAIN will not page to the end must not discard the hundred
                # already downloaded: a bare gather cancels its siblings on the first raised
                # exception, throwing away ten minutes of requests. Cancellation still does.
                except SyncCancelled, CatalogUnusableError:
                    raise
                except Exception as exc:  # noqa: BLE001 - named in the run's own report
                    lost.append(f"{dataset_id}: {_reason(exc)}")
                    log.warning("sync_all.dataset_failed", dataset=dataset_id, error=_reason(exc))
                    return
                out.extend(found)
                market["fields"] = len(out)
                await self._emit(run_id)

        await asyncio.gather(*(one(d.id) for d in datasets))
        if lost:
            # Kept, not raised: raising would send the whole market back through ``_retry``
            # and spend another 640 requests re-reading the 136 datasets that worked. The
            # fields that arrived are stored, and the ones that did not are named in the run
            # so the user is told rather than left with a quietly short catalog.
            self._all.get(run_id, {}).setdefault("failed", []).append(
                f"{target.label}: {len(lost)} of {len(datasets)} datasets could not be read "
                f"({'; '.join(lost[:3])})"
            )
        return out

    async def _crawl_all(
        self, run_id: int, targets: list[SyncTarget], cancel: asyncio.Event
    ) -> None:
        progress = self._all[run_id]
        totals = {"fields": 0, "datasets": 0}
        synced: list[SyncTarget] = []
        # Bounds how many markets hold a full field payload in memory at once.
        gate = asyncio.Semaphore(ALL_CONCURRENCY)
        # One unscoped read serves every market and runs beside the field downloads. Markets
        # never block on it for long: see BULK_DATASETS_WAIT.
        bulk_datasets = asyncio.create_task(self._all_datasets(targets))

        async def report() -> None:
            await self._update(
                run_id,
                fields_synced=totals["fields"],
                datasets_synced=totals["datasets"],
                error=_failures(progress),
            )
            await self._emit(run_id)

        async def pipeline(target: SyncTarget) -> None:
            market = progress["markets"][target.label]
            async with gate:
                _check(cancel)
                market["state"] = "fetching"
                await self._emit(run_id)

                async def store() -> tuple[int, int]:
                    started = time.perf_counter()
                    raw = await self._fields(target, bulk_datasets, market, run_id, cancel)
                    fetched = time.perf_counter()
                    _check(cancel)
                    if not raw:
                        raise RuntimeError("BRAIN returned no data fields")
                    now = utcnow()
                    rows = await asyncio.to_thread(_field_rows, raw, target, now)
                    datasets = await asyncio.to_thread(_dataset_rows, raw, target, now)
                    prepared = time.perf_counter()
                    scope = [target.instrument_type, target.region, target.delay, target.universe]
                    await self.catalog.replace_fields(scope, rows)
                    # Placeholders only fill gaps: a re-sync must not blank the Value Score,
                    # Coverage and counts that an earlier details step stored.
                    await self.catalog.upsert(
                        "data_set", DATASET_COLUMNS, datasets, overwrite=False
                    )
                    # Where a full sync's time goes: BRAIN's response, row building, or the
                    # single-writer DuckDB lock (which also waits on the other scopes' writes).
                    log.info(
                        "sync_all.scope_timing",
                        target=target.label,
                        fields=len(rows),
                        fetch_s=round(fetched - started, 2),
                        prepare_s=round(prepared - fetched, 2),
                        write_s=round(time.perf_counter() - prepared, 2),
                    )
                    return len(rows), len(datasets)

                try:
                    fields, datasets = await _retry(store, cancel, target.label)
                except SyncCancelled, CatalogUnusableError:
                    # An unusable catalog fails every remaining market the same way.
                    raise
                # One market failing must not end the whole sync.
                except Exception as exc:  # noqa: BLE001
                    market["state"] = "failed"
                    progress["failed"].append(f"{target.label}: {_reason(exc)}")
                    log.warning("sync_all.fields_failed", target=target.label, error=_reason(exc))
                    await report()
                    return

            # Browsable now; BRAIN's full dataset records follow straight away.
            totals["fields"] += fields
            totals["datasets"] += datasets
            market.update(state="details", fields=fields)
            synced.append(target)
            await report()

            async def details() -> None:
                now = utcnow()
                datasets = await _shared_datasets(bulk_datasets, target)
                if datasets is None:
                    # Counted per run: a market paying for its own scope means the shared
                    # read was too slow, and BULK_DATASETS_WAIT wants raising.
                    log.info("sync_all.datasets_fallback", target=target.label)
                    datasets = await self.endpoints.list_data_sets_all(**target.params)
                _check(cancel)
                await self.catalog.upsert(
                    "data_set",
                    DATASET_COLUMNS,
                    [_dataset_row(dataset, target, now) for dataset in datasets],
                )

            try:
                # Inside the gate too: dataset paging is many requests per market, and every
                # market paging at once drew 429s that outlasted all retries.
                async with gate:
                    await _retry(details, cancel, target.label)
                market["state"] = "done"
            except SyncCancelled, CatalogUnusableError:
                raise
            # The market stays browsable on its derived rows.
            except Exception as exc:  # noqa: BLE001
                market["state"] = "fields"
                progress["failed"].append(f"{target.label} details: {_reason(exc)}")
                log.warning("sync_all.details_failed", target=target.label, error=_reason(exc))
            await report()

        # Every market's pipeline at once, bounded by the gate above.
        await self._update(run_id, phase="fields")
        await self._emit(run_id)
        gathered = asyncio.gather(*(pipeline(t) for t in targets), return_exceptions=True)
        stopped = asyncio.create_task(cancel.wait())
        try:
            # A pipeline only sees the cancel at its next checkpoint, and one mid-download or
            # mid-retry is minutes from it. So Cancel ends the pipelines instead of waiting.
            await asyncio.wait({gathered, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if cancel.is_set():
                gathered.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gathered
                raise SyncCancelled
            results = gathered.result()
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, SyncCancelled):
                    raise result
        finally:
            stopped.cancel()
            # A shutdown lands on the wait above, not on the pipelines; they stop here, and are
            # waited for, so none outlives the run it belongs to.
            if not gathered.done():
                gathered.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gathered
            # Cancelling or failing the run must not leave the shared read holding a
            # connection, nor its exception unretrieved. CancelledError is named because it
            # is a BaseException: suppressing Exception alone lets it escape and strand the
            # run without a finished status.
            if not bulk_datasets.done():
                bulk_datasets.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await bulk_datasets

        # A market BRAIN stopped offering is never fetched, so nothing above replaces it: left,
        # it stays browsable and counted, and a Lab could spend simulations on it. Logged
        # rather than raised, like the compaction: it leaves only what was there before.
        try:
            removed = await self.catalog.prune_markets(
                [(t.instrument_type, t.region, t.delay, t.universe) for t in targets],
                region_agnostic=targets[0].region == REGION_AGNOSTIC_REGION,
            )
            if removed:
                log.info("sync_all.pruned", run_id=run_id, fields=removed)
        except Exception:
            log.warning("sync_all.prune_failed", run_id=run_id, exc_info=True)

        # Once, at the end: per market this would serialise behind every other market's
        # write. A failure here costs disk, not data, so it is logged rather than raised.
        try:
            await self.catalog.compact_fields()
        except Exception:
            log.warning("sync_all.compact_failed", run_id=run_id, exc_info=True)

        # New fields are unsearchable until they are indexed, and the index is cheap.
        try:
            await search.rebuild(self.catalog)
        except Exception:
            log.warning("sync_all.search_index_failed", run_id=run_id, exc_info=True)

        status = SyncStatus.COMPLETE if synced else SyncStatus.FAILED
        await self._finish(run_id, status, error=_failures(progress))
        log.info("sync_all.done", run_id=run_id, scopes=len(synced), failed=len(progress["failed"]))

    # -- run bookkeeping -------------------------------------------------

    async def _open_run(self, target: SyncTarget) -> SyncRun:
        async with self.db.session() as session:
            run = SyncRun(
                instrument_type=target.instrument_type,
                region=target.region,
                delay=target.delay,
                universe=target.universe,
                status=SyncStatus.RUNNING,
            )
            session.add(run)
            await session.flush()
            return run

    async def _update(self, run_id: int, **values: Any) -> None:
        async with self.db.session() as session:
            run = await session.get(SyncRun, run_id)
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)

    async def _finish(self, run_id: int, status: SyncStatus, *, error: str | None = None) -> None:
        await self._update(run_id, status=status, error=error, finished_at=utcnow())
        await self._emit(run_id)

    def _payload(self, run: SyncRun) -> dict[str, Any]:
        """A run's wire shape, plus live per-market states and progress while a full sync runs."""
        payload = serialise_run(run)
        progress = self._all.get(run.id)
        if progress:
            markets = list(progress["markets"].values())
            arrived = sum(m["state"] not in ("waiting", "fetching") for m in markets)
            payload |= {
                # "fields" while any market's fields are still on their way.
                "stage": "fields" if arrived < len(markets) else "details",
                "scopesDone": arrived,
                "scopesTotal": len(markets),
                "markets": markets,
                # One bar: each market's fields are half its share, its details the other half.
                "fraction": (
                    sum(_STEPS[m["state"]] for m in markets) / (2 * len(markets))
                    if markets
                    else 1.0
                ),
            }
        return payload

    async def _emit(self, run_id: int) -> None:
        run = await self.get_run(run_id)
        if run is None:
            return
        payload = self._payload(run)
        if (row := self._task_by_run.get(run_id)) is not None:
            done, total = payload.get("scopesDone"), payload.get("scopesTotal")
            await self.tasks.update(
                row,
                progress=payload.get("fraction"),
                detail=f"{done} of {total} markets" if total else "",
            )
        try:
            await self._on_progress(payload)
        except Exception:
            log.exception("sync.progress_hook_failed", run_id=run_id)


def _check(cancel: asyncio.Event) -> None:
    if cancel.is_set():
        raise SyncCancelled


async def _retry(work: Callable[[], Awaitable[Any]], cancel: asyncio.Event, label: str) -> Any:
    """Run one market's step up to ``SCOPE_ATTEMPTS`` times, waiting 2 s then 4 s between tries."""
    for attempt in range(1, SCOPE_ATTEMPTS + 1):
        _check(cancel)
        try:
            return await work()
        except SyncCancelled:
            raise
        except Exception as exc:
            if attempt == SCOPE_ATTEMPTS:
                raise
            log.warning("sync_all.retry", target=label, attempt=attempt, error=_reason(exc))
            await asyncio.sleep(2 * attempt)
    return None


# -- row builders ---------------------------------------------------------
# Tuple order must match the *_COLUMNS constants in db.duck.


#: The labs read MATRIX, VECTOR and GROUP and ignore the rest, so where BRAIN lists one id
#: under two types the usable one is the one worth keeping.
_TYPE_RANK = {"MATRIX": 0, "VECTOR": 1, "GROUP": 2}


async def _shared_datasets(
    task: asyncio.Task[dict[tuple[str, int, str], list[DataSet]] | None],
    target: SyncTarget,
) -> list[DataSet] | None:
    """This market's slice of the shared read, or ``None`` to go and fetch its own.

    Time-boxed: a slow unscoped read must never hold forty markets still, which is what a
    plain ``await`` on it did. ``asyncio.wait`` gives up on the task without cancelling it,
    so a later market still gets the answer.
    """
    if not task.done():
        await asyncio.wait([task], timeout=BULK_DATASETS_WAIT)
    if not task.done():
        return None
    indexed = task.result()
    return None if indexed is None else indexed.get((target.region, target.delay, target.universe))


def _field_rows(raw: list[BulkField], target: SyncTarget, now: datetime) -> list[tuple[Any, ...]]:
    """One tuple per field, read straight off the decoded structs.

    Keyed by field id: BRAIN repeats one in a few markets, and an upsert cannot touch the
    same row twice in a statement — DuckDB drops the extra and leaves its index inconsistent,
    which kills the connection on a later market's delete.
    """
    by_id: dict[str, tuple[Any, ...]] = {}
    ranks: dict[str, int] = {}
    instrument, region, delay, universe = (
        target.instrument_type,
        target.region,
        target.delay,
        target.universe,
    )
    for item in raw:
        rank = _TYPE_RANK.get(item.type or "", len(_TYPE_RANK))
        if item.id in ranks and ranks[item.id] <= rank:
            continue
        ranks[item.id] = rank
        dataset, category, subcategory = item.dataset, item.category, item.subcategory
        by_id[item.id] = (
            item.id,
            dataset.id if dataset else None,
            category.id if category else None,
            category.name if category else None,
            subcategory.id if subcategory else None,
            subcategory.name if subcategory else None,
            item.description,
            item.type,
            item.coverage,
            item.date_coverage,
            item.user_count,
            item.alpha_count,
            item.pyramid_multiplier,
            json.dumps(item.themes) if item.themes else None,
            item.date_created,
            item.region_coverage,
            instrument,
            region,
            delay,
            universe,
            now,
        )
    return list(by_id.values())


def _dataset_row(dataset: DataSet, target: SyncTarget, now: datetime) -> tuple[Any, ...]:
    return (
        dataset.id,
        dataset.name,
        dataset.description,
        dataset.category.id if dataset.category else None,
        dataset.category.name if dataset.category else None,
        dataset.subcategory.id if dataset.subcategory else None,
        dataset.subcategory.name if dataset.subcategory else None,
        dataset.coverage,
        dataset.value_score,
        dataset.user_count,
        dataset.alpha_count,
        dataset.field_count,
        dataset.pyramid_multiplier,
        json.dumps(dataset.themes) if dataset.themes else None,
        target.instrument_type,
        target.region,
        target.delay,
        target.universe,
        now,
    )


def _dataset_rows(raw: list[BulkField], target: SyncTarget, now: datetime) -> list[tuple[Any, ...]]:
    """Dataset rows built from the fields alone.

    Every field names its dataset, category and subcategory, so a scope is browsable before
    its details arrive. Read from the raw dicts rather than re-validating ~85k models.
    Stage 2 of a full sync overwrites these with BRAIN's full records.
    """
    datasets: dict[str, dict[str, Any]] = {}
    blank = FieldRef()
    for item in raw:
        dataset_id = (item.dataset or blank).id
        if dataset_id:
            entry = datasets.setdefault(
                dataset_id,
                {
                    "name": (item.dataset or blank).name,
                    "category": item.category or blank,
                    "sub": item.subcategory or blank,
                    "n": 0,
                },
            )
            entry["n"] += 1

    scope = (target.instrument_type, target.region, target.delay, target.universe)
    return [
        (
            dataset_id,
            e["name"],
            None,
            e["category"].id,
            e["category"].name,
            e["sub"].id,
            e["sub"].name,
            None,
            None,
            None,
            None,
            e["n"],
            None,
            None,
            *scope,
            now,
        )
        for dataset_id, e in datasets.items()
    ]


def _reason(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _failures(progress: dict[str, Any]) -> str | None:
    """Failed scopes as one line for the run's ``error``; None while nothing has failed."""
    failed: list[str] = progress["failed"]
    if not failed:
        return None
    shown = "; ".join(failed[:5])
    more = f"; and {len(failed) - 5} more" if len(failed) > 5 else ""
    return f"{len(failed)} failed: {shown}{more}"


def serialise_run(run: SyncRun) -> dict[str, Any]:
    """Wire shape for the API and the live progress feed."""
    everything = run.region == ALL_TARGET.region
    return {
        "id": run.id,
        "instrumentType": run.instrument_type,
        "region": run.region,
        "delay": run.delay,
        "universe": run.universe,
        "all": everything,
        "label": ALL_LABEL
        if everything
        else scope_label(run.instrument_type, run.region, run.delay, run.universe),
        "status": run.status,
        "phase": run.phase,
        "datasetsSynced": run.datasets_synced,
        "fieldsSynced": run.fields_synced,
        # A single market's field count is not known up front; the sync-all feed sets its own.
        "fraction": None,
        "error": run.error,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
    }
