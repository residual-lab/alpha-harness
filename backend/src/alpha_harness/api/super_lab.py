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
from ..labs.launch import AddedTask, add_study
from ..labs.params import SUPER_LAB, SuperParams
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/super-lab", tags=["super-lab"])


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


class Recipe(Out):
    name: str
    code: str


class SuperPlan(Out):
    selections: list[Recipe]
    combos: list[Recipe]
    decays: list[int]
    truncation: float
    simulations: int


def _activations(choice: str) -> list[str]:
    return ["IS", "OS"] if choice == "BOTH" else [choice]


@router.post("/plan")
async def plan(body: SuperTask) -> SuperPlan:
    """What a task with these settings would run, without running anything."""
    activations = _activations(body.activation)
    return SuperPlan(
        selections=[
            Recipe(name=r.name, code=r.code) for r in super_alpha.selections(body.selection_limit)
        ],
        combos=[Recipe(name=r.name, code=r.code) for r in super_alpha.COMBOS],
        decays=list(super_alpha.DECAYS),
        truncation=super_alpha.TRUNCATION,
        simulations=len(super_alpha.selections(body.selection_limit))
        * len(super_alpha.COMBOS)
        * len(super_alpha.DECAYS)
        * len(activations),
    )


@router.post("/tasks", status_code=201)
async def add_task(body: SuperTask, state: State) -> AddedTask:
    """Write every pairing as a SuperAlpha simulation, and queue the task when ``run``."""
    if body.region == REGION_AGNOSTIC_REGION:
        raise refuse(422, "region_agnostic", "SuperAlphas combine Alphas of one region.")
    found = super_alpha.candidates(
        region=body.region,
        delay=body.delay,
        universe=body.universe,
        neutralization=body.neutralization,
        selection_limit=body.selection_limit,
        activations=_activations(body.activation),
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
        template_source=super_alpha.COMBOS[0].code,
        template_name=f"Super Alpha Lab · {body.region} D{body.delay} {body.universe}",
        run=body.run,
        seeds=super_alpha.seed_trials(found),
    )
