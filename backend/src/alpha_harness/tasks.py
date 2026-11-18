"""What is running right now.

One registry every long background job reports into, so the interface has a single
answer to "is anything happening?" rather than a per-job progress readout.

Deliberately in-memory: a task is a thing happening *now*, and a task list that survives
the work it describes would be worse than none.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from .brain.connection import background
from .schemas import camel_dict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)

#: Finished tasks stay visible this long, so a job that completes between two polls is
#: still seen rather than vanishing as though it never ran.
LINGER_SECONDS = 60.0


@dataclass(slots=True)
class Task:
    """One long-running piece of work."""

    id: str
    kind: str
    label: str
    #: 0.0-1.0 where it is knowable, ``None`` where it genuinely is not.
    progress: float | None = None
    detail: str = ""
    state: str = "running"  # running | done | failed | cancelled
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    #: Free-form, for the thing that owns the task — a run id, a scope label.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def to_dict(self) -> dict[str, Any]:
        return camel_dict(self, exclude=("started", "finished")) | {
            "elapsedSeconds": round(self.elapsed, 1)
        }


class TaskRegistry:
    """Every background job, in one place.

    No lock: every mutation here is a single dict operation with no ``await`` in it, so
    the event loop cannot interleave two of them.
    """

    def __init__(self, on_change: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._tasks: dict[str, Task] = {}
        self._on_change = on_change

    async def start(self, kind: str, label: str, **meta: Any) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], kind=kind, label=label, meta=meta)
        self._tasks[task.id] = task
        await self._notify()
        return task

    async def update(
        self,
        task: Task,
        *,
        progress: float | None = None,
        detail: str | None = None,
        **meta: Any,
    ) -> None:
        if progress is not None:
            task.progress = max(0.0, min(1.0, progress))
        if detail is not None:
            task.detail = detail
        task.meta.update(meta)
        await self._notify()

    async def finish(self, task: Task, *, state: str = "done", error: str | None = None) -> None:
        task.state = state
        task.error = error
        task.finished = time.monotonic()
        if state == "done" and task.progress is not None:
            task.progress = 1.0
        await self._notify()

    def list_tasks(self) -> list[dict[str, Any]]:
        self._prune()
        tasks = sorted(self._tasks.values(), key=lambda t: t.started)
        return [t.to_dict() for t in tasks]

    def summary(self) -> dict[str, Any]:
        """What the indicator in the top bar needs: are we busy, and with what."""
        tasks = self.list_tasks()
        running = [t for t in tasks if t["state"] == "running"]
        known = [t["progress"] for t in running if t["progress"] is not None]
        return {
            "busy": bool(running),
            "running": len(running),
            "failed": len([t for t in tasks if t["state"] == "failed"]),
            # Averaged only over tasks that can report progress; a sync that knows it is
            # 40% done should not be dragged toward zero by one that cannot say.
            "progress": (sum(known) / len(known)) if known else None,
            "tasks": tasks,
        }

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [
            k
            for k, t in self._tasks.items()
            if t.finished is not None and now - t.finished > LINGER_SECONDS
        ]:
            del self._tasks[key]

    async def _notify(self) -> None:
        await self._on_change(self.summary())


#: Detached jobs started from request handlers and labs, held so none is collected mid
#: flight and all can be cancelled before the stores they use are closed.
_background: set[asyncio.Task[Any]] = set()


def spawn[T](coro: Awaitable[T], *, name: str) -> asyncio.Task[T | None]:
    """Run ``coro`` detached, tracked for :func:`cancel_background`."""

    async def run() -> T | None:
        # Detached work runs on its own, so it stands down while BRAIN cannot be asked.
        background()
        try:
            return await coro
        # Nobody awaits a detached job, so an uncaught error would otherwise vanish.
        except Exception:
            log.exception("background.failed", name=name)
            return None

    task = asyncio.create_task(run(), name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def cancel_background() -> None:
    """Cancel every detached job and wait for each to unwind. Called first on shutdown."""
    for task in list(_background):
        task.cancel()
    await asyncio.gather(*_background, return_exceptions=True)
    _background.clear()
