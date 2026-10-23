"""Super Alpha Lab: SuperAlphas built from your own submitted Alphas, ready to submit.

A SuperAlpha is a selection expression, ranking every submitted ``ACTIVE`` Alpha of the
region and delay and keeping the top ``selectionLimit``, and a combo expression, weighting
the kept Alphas each day. Nothing here searches either expression: SuperAlpha simulations
are slow and BRAIN runs three at a time, so the lab crosses a small library of
economically grounded selections with proven weighting schemes, each at two decays to land
Turnover inside the 2% to 40% a SuperAlpha must clear, and lets BRAIN's own checks say
which are submittable.

Every selection caps each Alpha's operators at 8,000 / limit: BRAIN warns rather than
fails past 8,000 operators in all, and drops what does not fit.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..brain.platform import SUPER_ALPHA_MIN_SELECTION, SUPER_ALPHA_OPERATOR_LIMIT
from ..brain.schemas import FULL_MODE, SimulationRequest, SimulationSettings, SimulationType
from ..db.models import Trial, TrialState
from .scheduler import PENDING_SEND

if TYPE_CHECKING:
    from collections.abc import Callable

#: Combo smoothing, each pairing at both: lighter keeps fast components' edge, heavier pulls an
#: over-trading combination under the 40% Turnover ceiling.
DECAYS = (4, 10)
#: Applied to the final weighted combination. Lower than an Alpha's usual 0.08: a SuperAlpha
#: concentrates more easily, and truncation is the documented brake on overfitting.
TRUNCATION = 0.05


@dataclass(frozen=True, slots=True)
class Recipe:
    name: str
    code: str


def selections(limit: int) -> list[Recipe]:
    """Rankings over Alpha properties, each one keeping the pool diverse in its own way.

    ``{cap}`` bounds an Alpha's operators so the kept ones never pass 8,000 between them.
    Products of gates and scores: a gate that fails scores 0, which ``POSITIVE`` drops.
    """
    cap = SUPER_ALPHA_OPERATOR_LIMIT // max(limit, SUPER_ALPHA_MIN_SELECTION)
    budget = f"(operator_count <= {cap})"
    return [
        Recipe(
            "Least correlated",
            f"{budget} * (self_correlation < 0.7) * (1 - prod_correlation)",
        ),
        Recipe(
            "Tradeable turnover",
            f"{budget} * (turnover >= 0.02) * (turnover < 0.35) * (1 - self_correlation)",
        ),
        Recipe(
            "Broad books",
            f"{budget} * (long_count + short_count) / sqrt(universe_size(universe))",
        ),
        Recipe(
            "Slow core",
            f"{budget} * (turnover < 0.25) / (turnover + 0.05)",
        ),
        Recipe(
            "Many sources",
            f"{budget} * (1 + datacategory_count) * (1 - prod_correlation)",
        ),
    ]


#: Weighting schemes from BRAIN's own combo examples and their risk-aware variants.
COMBOS = [
    Recipe("Equal weight", "1"),
    # Rolling IR as mean / std dev: ``ts_ir`` is not offered to every account, and BRAIN
    # rejects a combo using it ("inaccessible or unknown operator").
    Recipe(
        "Rolling Sharpe",
        "stats = generate_stats(alpha);\n"
        "max(ts_mean(stats.returns, 250) / (ts_std_dev(stats.returns, 250) + 0.0001), 0)",
    ),
    Recipe(
        "Least crowded",
        "stats = generate_stats(alpha);\n"
        "innerCorr = self_corr(stats.returns, 500);\n"
        "ic = if_else(innerCorr == 1.0, nan, innerCorr);\n"
        "maxCorr = reduce_max(ic);\n"
        "max(1 - maxCorr, 0.05)",
    ),
    Recipe(
        "Inverse volatility",
        "stats = generate_stats(alpha);\n1 / (ts_std_dev(stats.returns, 120) + 0.0001)",
    ),
]


@dataclass(frozen=True, slots=True)
class Candidate:
    selection: Recipe
    combo: Recipe
    request: SimulationRequest

    @property
    def name(self) -> str:
        return f"{self.selection.name} · {self.combo.name}"


def candidates(
    *,
    region: str,
    delay: int,
    universe: str,
    neutralization: str,
    selection_limit: int,
    activations: list[str],
) -> list[Candidate]:
    """Every selection · combo · decay · activation, as Full SUPER simulations.

    Full mode always: only a Full simulation can be submitted, and Quick runs none of the
    correlation checks a SuperAlpha must also pass.
    """
    out: list[Candidate] = []
    for activation in activations:
        for decay in DECAYS:
            for selection in selections(selection_limit):
                for combo in COMBOS:
                    settings = SimulationSettings(
                        region=region,
                        delay=delay,
                        universe=universe,
                        neutralization=neutralization,
                        decay=decay,
                        truncation=TRUNCATION,
                        selection_handling="POSITIVE",
                        selection_limit=selection_limit,
                        component_activation=activation,
                        simulation_mode=FULL_MODE,
                    )
                    request = SimulationRequest(
                        type=SimulationType.SUPER,
                        settings=settings,
                        selection=selection.code,
                        combo=combo.code,
                    )
                    out.append(Candidate(selection, combo, request))
    return out


def seed_trials(found: list[Candidate]) -> Callable[[int], list[Trial]]:
    """Parked trials for every candidate. Both expressions ride in ``params``, which
    ``send_parked`` reads; the trial's own expression names the recipe, the line a results
    table shows."""

    def build(study_id: int) -> list[Trial]:
        return [
            Trial(
                study_id=study_id,
                number=number,
                params={
                    "recipe": candidate.name,
                    "selection": candidate.request.selection,
                    "combo": candidate.request.combo,
                },
                distributions={},
                expression=(
                    f"{candidate.name} · Decay {candidate.request.settings.decay}"
                    f" · {candidate.request.settings.component_activation}"
                ),
                settings=candidate.request.settings.model_dump(by_alias=True, exclude_none=True),
                state=TrialState.PRUNED,
                message=PENDING_SEND,
            )
            for number, candidate in enumerate(found)
        ]

    return build


if __name__ == "__main__":
    import sys

    from ..engine.lifecycle import request_hash
    from ..engine.packer import key_of
    from .scheduler import request_of

    limit = 40
    found = candidates(
        region="USA",
        delay=1,
        universe="TOP3000",
        neutralization="SUBINDUSTRY",
        selection_limit=limit,
        activations=["IS", "OS"],
    )
    trials = seed_trials(found)(1)
    wires = [c.request.to_wire() for c in found]
    checks = {
        "every pairing at every decay and activation": len(found)
        == len(selections(limit)) * len(COMBOS) * len(DECAYS) * 2,
        "no two alike": len({request_hash(c.request) for c in found}) == len(found),
        "operators capped at 8,000 / limit": all(
            f"(operator_count <= {SUPER_ALPHA_OPERATOR_LIMIT // limit})" in r.code
            for r in selections(limit)
        ),
        "SUPER, Full, Positive, as asked": all(
            w["type"] == "SUPER"
            and w["settings"]["simulationMode"] == FULL_MODE
            and w["settings"]["selectionHandling"] == "POSITIVE"
            and w["settings"]["selectionLimit"] == limit
            and w["selection"]
            and w["combo"]
            for w in wires
        ),
        "a written trial sends the same simulation": all(
            request_hash(request_of(t)) == request_hash(c.request)
            for t, c in zip(trials, found, strict=True)
        ),
        "never batched": all(key_of(w).max_batch == 1 for w in wires),
    }
    if failed := [check for check, ok in checks.items() if not ok]:
        raise SystemExit(f"super alpha self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(found)} SuperAlpha simulations, e.g. {trials[0].expression}\n")
