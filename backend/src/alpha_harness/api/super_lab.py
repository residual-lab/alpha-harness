"""Super Alpha Lab: one form, one task of SuperAlphas from your own submitted Alphas."""

from typing import Literal

from fastapi import APIRouter
from pydantic import Field

from ..brain.platform import (
    MULTI_SIMULATION_SIZE,
    REGION_AGNOSTIC_REGION,
    SUPER_ALPHA_MIN_SELECTION,
    SUPER_ALPHA_SLOTS,
)
from ..db.models import utcnow
from ..labs import super_alpha
from ..labs.launch import AddedTask, account_operators, add_study
from ..labs.params import SUPER_LAB, SuperParams
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/super-lab", tags=["super-lab"])


class Recipe(Out):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=8000)


class SuperTask(Out):
    region: str
    delay: int
    universe: str
    neutralization: str
    selection_limit: int = Field(default=30, ge=SUPER_ALPHA_MIN_SELECTION, le=200)
    #: ``BOTH`` runs every pairing under each Component Activation.
    activation: Literal["IS", "OS", "BOTH"] = "BOTH"
    cores: int = Field(default=SUPER_ALPHA_SLOTS, ge=1, le=SUPER_ALPHA_SLOTS)
    run: bool = False
    #: The selections and combos to cross: built-in, edited or the consultant's own. Left out,
    #: every built-in one.
    selections: list[Recipe] | None = Field(default=None, max_length=50)
    combos: list[Recipe] | None = Field(default=None, max_length=50)

    def chosen(
        self, combos: list[super_alpha.Recipe]
    ) -> tuple[list[super_alpha.Recipe], list[super_alpha.Recipe]]:
        """The selections and combos to cross, as the lab's own recipes; ``combos`` are the
        built-in ones as this account runs them."""
        picked = [
            [super_alpha.Recipe(r.name.strip(), r.code.strip()) for r in given]
            if given is not None
            else default
            for given, default in (
                (self.selections, super_alpha.selections(self.selection_limit)),
                (self.combos, combos),
            )
        ]
        return picked[0], picked[1]


class SuperPlan(Out):
    selections: list[Recipe]
    combos: list[Recipe]
    decays: list[int]
    truncation: float
    simulations: int


def _activations(choice: str) -> list[str]:
    return ["IS", "OS"] if choice == "BOTH" else [choice]


@router.post("/plan")
async def plan(body: SuperTask, state: State) -> SuperPlan:
    """What a task with these settings would run, without running anything. Lists the
    built-in selections and combos whatever was chosen, so a screen can offer them all."""
    activations = _activations(body.activation)
    combos = super_alpha.combos_for(await account_operators(state, refresh=False))
    chosen = body.chosen(combos)
    return SuperPlan(
        selections=[
            Recipe(name=r.name, code=r.code) for r in super_alpha.selections(body.selection_limit)
        ],
        combos=[Recipe(name=r.name, code=r.code) for r in combos],
        decays=list(super_alpha.DECAYS),
        truncation=super_alpha.TRUNCATION,
        simulations=len(chosen[0]) * len(chosen[1]) * len(super_alpha.DECAYS) * len(activations),
    )


@router.post("/tasks", status_code=201)
async def add_task(body: SuperTask, state: State) -> AddedTask:
    """Write every pairing as a SuperAlpha simulation, and queue the task when ``run``."""
    if body.region == REGION_AGNOSTIC_REGION:
        raise refuse(422, "region_agnostic", "SuperAlphas combine Alphas of one region.")
    selections, combos = body.chosen(
        super_alpha.combos_for(await account_operators(state, refresh=False))
    )
    for kind, chosen in (("selection", selections), ("combo", combos)):
        if not chosen:
            raise refuse(422, f"no_{kind}", f"Tick at least one {kind}.")
        names = [r.name for r in chosen]
        if len(set(names)) < len(names):
            # A result is named for its selection and combo, so two of one name read as one.
            raise refuse(422, f"duplicate_{kind}", f"Two {kind}s share a name: rename one.")
    found = super_alpha.candidates(
        region=body.region,
        delay=body.delay,
        universe=body.universe,
        neutralization=body.neutralization,
        selection_limit=body.selection_limit,
        activations=_activations(body.activation),
        chosen_selections=selections,
        combos=combos,
    )
    return await add_study(
        state,
        now=utcnow(),
        sampler=SUPER_LAB,
        params=SuperParams(
            region=body.region,
            delay=body.delay,
            universe=body.universe,
            neutralization=body.neutralization,
            selection_limit=body.selection_limit,
            activation=_activations(body.activation),
            recipes=sorted({c.name for c in found}),
            cores=body.cores,
        ),
        simulations=len(found),
        batch_size=body.cores * MULTI_SIMULATION_SIZE,
        template_source=combos[0].code,
        template_name=f"Super Alpha Lab · {body.region} D{body.delay} {body.universe}",
        run=body.run,
        seeds=super_alpha.seed_trials(found),
    )
