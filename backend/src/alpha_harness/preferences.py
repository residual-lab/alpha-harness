"""What the consultant chooses on the Settings screen, kept in ``harness.db``.

Every choice has a default, so an install that never opens Settings runs as the app does
out of the box.
"""

from typing import TYPE_CHECKING, Literal

from pydantic import Field
from sqlalchemy import select

from . import updates
from .brain.schemas import CheckResult
from .db.models import Preference
from .schemas import Out

if TYPE_CHECKING:
    from .db.sqlite import Database
    from .state import AppState


class Preferences(Out):
    #: Lend cores no running task holds to the tasks with more work than their own cores.
    #: Off by default: a task's cores are otherwise a ceiling as well as a reservation.
    lend_idle_cores: bool = False
    #: Ask the operating system not to sleep while simulations are pending.
    keep_awake: bool = True
    #: Regions whose simulations run in BRAIN's Quick mode: the same figures and the checks
    #: that score an Alpha, without correlation, Theme or Competition. GLB by default, where a
    #: batch was measured finishing 30% sooner; a Quick Alpha that passes is then run in Full.
    quick_regions: list[str] = Field(default_factory=lambda: ["GLB"], max_length=20)
    #: Download an Alpha's daily PnL as soon as it lands, when every check that gates it reads
    #: one of ``pnl_check_results``. A series a screen asks for still downloads either way.
    pnl_download: bool = True
    #: The default is the Alphas nothing refused, which is what was downloaded before this was
    #: a choice.
    pnl_check_results: list[CheckResult] = Field(
        default_factory=lambda: [CheckResult.PASS, CheckResult.WARNING, CheckResult.PENDING]
    )
    #: Cores a lab or tool starts a new task with, until changed in its own form.
    default_cores: int = Field(default=4, ge=1, le=8)
    #: Hours between looks at GitHub for a new release; 0 looks only when asked.
    update_check_hours: Literal[0, 1, 6, 24] = 1
    #: Install a new release by itself, once nothing is simulating.
    auto_update: bool = True
    #: The model that writes Power Pool descriptions, as ``provider:model``; unset, the set-up
    #: model with the largest daily allowance.
    description_model: str | None = None


async def load(db: Database) -> Preferences:
    """Every choice, defaults filled in. Rows a newer build wrote are ignored."""
    async with db.session() as session:
        rows = await session.scalars(select(Preference))
        return Preferences.model_validate({row.key: row.value for row in rows})


async def save(db: Database, preferences: Preferences) -> None:
    async with db.session() as session:
        # Stored under the Python names, which never change with the wire's spelling.
        for key, value in preferences.model_dump(mode="json", by_alias=False).items():
            await session.merge(Preference(key=key, value=value))


def apply(state: AppState, preferences: Preferences) -> None:
    """Hand every choice to the part of the app that acts on it. Takes effect at once."""
    state.engine.lend_idle_cores = preferences.lend_idle_cores
    state.engine.set_keep_awake(preferences.keep_awake)
    state.engine.quick_regions = frozenset(preferences.quick_regions)
    state.backfill.pnl_results = (
        frozenset(preferences.pnl_check_results) if preferences.pnl_download else frozenset()
    )
    updates.check_hours = preferences.update_check_hours
    updates.auto_install = preferences.auto_update
