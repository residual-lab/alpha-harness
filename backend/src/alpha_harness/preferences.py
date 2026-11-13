"""What the consultant chooses on the Settings screen, kept in ``harness.db``.

Every choice has a default, so an install that never opens Settings runs as the app does
out of the box. The recipes a consultant writes in a lab are kept here too, one row a lab,
so a reinstall or another browser still finds them.
"""

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field
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
    #: Tag every Alpha a lab makes on BRAIN, as ACE's notebooks do, so it can be found there.
    #: Off by default: each tag is one more request to BRAIN.
    auto_tag: bool = False
    #: The tag, written beside "<tag>_<lab>" for the lab that made the Alpha.
    auto_tag_name: str = Field(
        default="harness", min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$"
    )


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


class OwnRecipe(Out):
    """A selection, combo or re-shape the consultant wrote, as their screen holds it."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    #: Blank while it is being typed; the lab running it refuses a blank one then.
    name: str = Field(max_length=120)
    code: str = Field(max_length=8000)


class SuperLabRecipes(Out):
    """The Super Alpha Lab's own selections and combos, and which of every one are unticked."""

    selections: list[OwnRecipe] = Field(default_factory=list[OwnRecipe], max_length=50)
    combos: list[OwnRecipe] = Field(default_factory=list[OwnRecipe], max_length=50)
    #: ``builtin:{name}`` or ``custom:{id}``.
    off: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list[str], max_length=200
    )


class BreakerReshapes(Out):
    """The Correlation Breaker's own re-shapes, offered for every Alpha."""

    reshapes: list[OwnRecipe] = Field(default_factory=list[OwnRecipe], max_length=50)


class TagGroup(Out):
    """Tags the Alphas screen filters by together, kept under a name: a list of Alphas."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=60)
    tags: list[Annotated[str, Field(max_length=40)]] = Field(min_length=1, max_length=20)
    #: Alphas carrying every tag rather than any one of them.
    every: bool = False


class TagGroups(Out):
    groups: list[TagGroup] = Field(default_factory=list[TagGroup], max_length=50)


#: Their rows in ``preference``, beside the Settings screen's choices, which never read them.
SUPER_LAB = "super_lab_recipes"
BREAKER = "correlation_breaker_reshapes"
TAG_GROUPS = "alpha_tag_groups"


async def kept[M: BaseModel](db: Database, key: str, model: type[M]) -> M | None:
    """What a screen saved under ``key``; ``None`` until it first saves."""
    async with db.session() as session:
        row = await session.get(Preference, key)
        return None if row is None else model.model_validate(row.value)


async def keep(db: Database, key: str, value: BaseModel) -> None:
    async with db.session() as session:
        await session.merge(
            Preference(key=key, value=value.model_dump(mode="json", by_alias=False))
        )


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
    state.autotag.name = preferences.auto_tag_name if preferences.auto_tag else None


if __name__ == "__main__":
    import asyncio
    import sys
    import tempfile
    from pathlib import Path
    from typing import Any

    from pydantic import ValidationError

    from .db.sqlite import Database

    def refused(model: type[BaseModel], data: dict[str, Any]) -> bool:
        try:
            model.model_validate(data)
        except ValidationError:
            return True
        return False

    blank = {"id": "a-1", "name": "", "code": ""}

    async def round_trip() -> dict[str, bool]:
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "harness.db")
            await db.create_all()
            before = await kept(db, BREAKER, BreakerReshapes)
            mine = BreakerReshapes.model_validate({"reshapes": [{**blank, "code": "rank(alpha)"}]})
            await keep(db, BREAKER, mine)
            after = await kept(db, BREAKER, BreakerReshapes)
            other = await kept(db, SUPER_LAB, SuperLabRecipes)
            settings = await load(db)
            await db.dispose()
        return {
            "nothing until first saved": before is None,
            "read back as saved, a blank name included": after == mine,
            "one lab's row is not another's": other is None,
            "never read as a Settings choice": settings == Preferences(),
        }

    checks = {
        **asyncio.run(round_trip()),
        "a 51st re-shape refused": refused(
            BreakerReshapes, {"reshapes": [{**blank, "id": str(i)} for i in range(51)]}
        ),
        "an id that is not a token refused": refused(OwnRecipe, {**blank, "id": "../a"}),
        "code past 8,000 characters refused": refused(OwnRecipe, {**blank, "code": "x" * 8001}),
        "an overlong unticked key refused": refused(SuperLabRecipes, {"off": ["x" * 201]}),
        "a browser's copy as it stands accepted": not refused(
            SuperLabRecipes,
            {"selections": [blank], "combos": [], "off": ["builtin:Slow core", "custom:a-1"]},
        ),
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"preferences self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
