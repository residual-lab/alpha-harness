"""Simulations and their resulting alphas."""

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..db.models import Study, StudyStatus
from ..engine.lifecycle import serialise
from ..labs import scheduler
from ..labs.params import TASK_SAMPLERS
from ..schemas import (
    CancelResult,
    DropResult,
    EngineStatus,
    SimulationRow,
)
from .deps import State

router = APIRouter(prefix="/api", tags=["simulations"])


@router.get("/simulations/engine")
async def engine_status(state: State) -> EngineStatus:
    """Slot occupancy, queue depth and task quotas — what the matrix header shows."""
    return EngineStatus.model_validate(await state.engine.status())


@router.delete("/simulations/queue")
async def drop_queue(state: State, task: str | None = None) -> DropResult:
    """Pause the tasks whose simulations are queued, and drop queued work no task owns.

    Paused rather than only emptied: a running task queues the same work again at once. Its
    simulations wait for it to run again; nothing queued was sent, so nothing was spent.
    """
    statement = select(Study.id).where(
        Study.sampler.in_(TASK_SAMPLERS),
        Study.status.in_([StudyStatus.RUNNING, StudyStatus.QUEUED]),
    )
    if task is not None:
        statement = statement.where(Study.task == task)
    async with state.db.session() as session:
        owners = list((await session.scalars(statement)).all())
    dropped = 0
    for study_id in owners:
        dropped += await scheduler.pause(state.optimizer, study_id) or 0
    return DropResult(dropped=dropped + await state.engine.drop_queued(task))


@router.get("/simulations/active")
async def active(state: State) -> list[SimulationRow]:
    """Everything currently pending or running — what the matrix renders."""
    return [SimulationRow.model_validate(serialise(r)) for r in await state.tracker.active()]


@router.post("/simulations/{record_id}/cancel")
async def cancel(record_id: int, state: State) -> CancelResult:
    """Cancel a running simulation.

    Uses the stored platform id — the reason that id is written to disk before the
    submission request is even sent.
    """
    record = await state.tracker.get(record_id)
    if record is None:
        raise HTTPException(404, "No such simulation")

    acknowledged = await state.tracker.cancel(record_id)
    updated = await state.tracker.get(record_id)
    return CancelResult(
        acknowledged=acknowledged,
        simulation=SimulationRow.model_validate(serialise(updated)) if updated else None,
    )
