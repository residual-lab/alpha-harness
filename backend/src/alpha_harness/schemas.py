"""The base every response model shares, and the two shapes every caller repeats.

The frontend reads camelCase; Python writes snake_case. Declaring that mapping once
means a field added to a model reaches the wire on its own.

Lives at the top of the package rather than under ``api/`` so domain modules can build
their own response models without importing the layer that serves them.
"""

from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .db.models import SimStatus, SyncStatus

if TYPE_CHECKING:
    from collections.abc import Container


#: camelCase out, either spelling in. Also the config of pydantic dataclasses on the wire.
WIRE = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class Out(BaseModel):
    """Serialises to camelCase, accepts either spelling on the way in."""

    model_config = WIRE


def camel_dict(obj: Any, exclude: Container[str] = ()) -> dict[str, Any]:
    """A dataclass on the wire, in the same camelCase :class:`Out` produces.

    For working objects that stay dataclasses because :class:`Out` would put validation
    on every assignment in a hot path.

    ``exclude`` drops fields internal to the calculation. Merge in anything computed
    rather than stored: ``camel_dict(self) | {"canMultiSimulate": self.can_multi_simulate}``.
    """
    return {to_camel(k): v for k, v in asdict(obj).items() if k not in exclude}


# --- simulations: the typed pilot for generated frontend types ---------------------


class SimulationRow(Out):
    """One simulation record. A multi-simulation parent holds one of the 8 slots."""

    id: int
    platform_id: str | None
    alpha_id: str | None
    status: SimStatus
    platform_status: str | None
    #: 0..1, reported on batch parents and standalone simulations only.
    progress: float | None
    message: str | None
    #: A batch parent reads "<n> simulations".
    expression: str | None
    task: str
    region: str
    delay: int
    universe: str | None
    instrument_type: str
    language: str
    sim_type: str
    is_batch: bool
    child_ids: list[str]
    #: The batch parent's record id; null for standalone simulations and batch parents.
    parent_id: int | None
    created_at: str | None
    #: Identical on a batch parent and its children; the only link between them.
    submitted_at: str | None
    finished_at: str | None
    elapsed_seconds: float | None
    #: BRAIN's own settings keys, passed through as sent.
    settings: dict[str, Any] | None


class Pause(Out):
    start: datetime
    end: datetime


class EngineStatus(Out):
    """Slot occupancy, queue depth and task quotas — what the matrix header shows."""

    slots: int
    #: 10, or 1 without the MULTI_SIMULATION permission.
    max_batch: int
    slots_used: int
    slots_free: int
    #: Task name -> queued simulations.
    queued: dict[str, int]
    queued_total: int
    #: Task name -> slots held.
    in_flight: dict[str, int]
    quotas: dict[str, int]
    daily_limit_hit: bool
    #: BRAIN refused the session; sending waits until it is renewed or the user signs in.
    session_lost: bool
    #: OS sleep block: held while work is pending, unavailable where the OS cannot be asked.
    awake: Literal["idle", "held", "unavailable"]
    #: The account holds QUICK_MODE, so the regions Settings name run in Quick mode.
    quick_allowed: bool
    #: The last time this computer slept while the engine ran, which paused sending.
    last_pause: Pause | None
    #: Rough minutes until the queue empties, from recent completions; none when unknown.
    minutes_left: int | None


class SyncRunRow(Out):
    """One catalog download, as :func:`alpha_harness.catalog.sync.serialise_run` writes it."""

    id: int
    instrument_type: str
    region: str
    delay: int
    universe: str
    #: The one run that downloads every market at once.
    all: bool
    label: str
    status: SyncStatus
    phase: Literal["categories", "datasets", "fields", "details"] | None
    datasets_synced: int
    fields_synced: int
    fraction: float | None
    error: str | None
    started_at: str | None
    finished_at: str | None


class SyncMarket(Out):
    """One market's state inside a whole-catalog sync, for the sync matrix."""

    region: str
    delay: int
    universe: str
    state: Literal["waiting", "fetching", "fields", "details", "done", "failed"]
    fields: int | None


class SyncAllRun(SyncRunRow):
    """The whole-catalog run, with every market's live state."""

    stage: Literal["fields", "details"]
    scopes_done: int
    scopes_total: int
    markets: list[SyncMarket]


class CancelResult(Out):
    acknowledged: bool
    simulation: SimulationRow | None


class DropResult(Out):
    dropped: int
