"""What a study is trying to maximise: Sharpe, fitness, train fitness, read from a finished
alpha's statistics."""

from dataclasses import dataclass
from typing import Any

from ..brain.schemas import QUICK_MODE, Alpha, SampleStats
from ..vault.yields import without_quota_checks

#: A missing number scores this: deliberately terrible, so a broken trial is never mistaken
#: for a good one. Every objective is maximised.
FAILURE = -10.0


class StudyError(ValueError):
    """A study configured in a way that cannot work. Understood and refused."""


class StudyNotFoundError(StudyError):
    """No such study. Separate because "does not exist" is not "cannot work"."""

    def __init__(self, study_id: int | str) -> None:
        super().__init__(f"No study {study_id!r}.")
        self.study_id = study_id


@dataclass(frozen=True, slots=True)
class Objective:
    key: str
    label: str


OBJECTIVES: dict[str, Objective] = {
    "sharpe": Objective("sharpe", "Sharpe"),
    # Over the train years only, so the held-out test years stay unseen by the search.
    "train_sharpe": Objective("train_sharpe", "Train Sharpe"),
    "train_fitness": Objective("train_fitness", "Train Fitness"),
}


def resolve(keys: list[str]) -> list[Objective]:
    unknown = [k for k in keys if k not in OBJECTIVES]
    if unknown:
        raise StudyError(
            f"Unknown objective(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(OBJECTIVES))}."
        )
    if not keys:
        raise StudyError("A study needs at least one objective.")
    return [OBJECTIVES[k] for k in keys]


def extract(
    stats: SampleStats | None,
    objectives: list[Objective],
    extra: dict[str, float | None] | None = None,
) -> list[float]:
    """Objective values from an alpha's in-sample statistics.

    ``extra`` carries numbers the statistics block does not have, such as the train-years
    fitness. A missing number becomes the objective's failure value: Optuna needs one number
    per objective, and a zero would make a broken simulation look mediocre instead of bad.
    """
    values: list[float] = []
    for objective in objectives:
        if extra is not None and objective.key in extra:
            raw = extra[objective.key]
        else:
            raw = _value(stats, objective.key)
        values.append(FAILURE if raw is None else float(raw))
    return values


def _value(stats: SampleStats | None, key: str) -> float | None:
    if stats is None:
        return None
    value = getattr(stats, key, None)
    return None if value is None else float(value)


def train_extra(alpha: Alpha, objectives: list[Objective]) -> dict[str, float | None]:
    """Objectives read from the ``train`` block.

    A simulation with a test period reports its train years there, while ``is`` stays the
    whole period.
    """
    return {
        o.key: getattr(alpha.train, o.key.removeprefix("train_"), None) if alpha.train else None
        for o in objectives
        if o.key.startswith("train_")
    }


def summarise(alpha: Alpha) -> dict[str, Any]:
    """The per-trial result row the UI shows, with nothing dropped."""
    stats = alpha.in_sample
    # Shaped as the vault stores them, so a trial reads the same whichever way it was scored.
    checks = without_quota_checks(
        [
            {
                "name": c.name,
                "result": str(c.result) if c.result else None,
                "value": c.value,
                "limit": c.limit,
            }
            for c in (stats.checks if stats else [])
        ]
    )
    return {
        "alphaId": alpha.id,
        "grade": alpha.grade,
        "stats": stats.model_dump(by_alias=True, exclude={"checks"}) if stats else None,
        "checks": checks,
        "failedChecks": [c["name"] for c in checks if c["result"] == "FAIL"],
        "quick": alpha.settings is not None and alpha.settings.simulation_mode == QUICK_MODE,
    }
