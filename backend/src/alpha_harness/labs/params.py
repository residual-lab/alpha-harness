"""What each lab stores in ``Study.sampler_params``, typed.

The column stays JSON, and existing rows were written before these models: keys keep the
spelling they were stored with (``datasetIds`` beside ``n_startup_trials``), unknown keys
survive a read and a write (``extra="allow"``), and every field a reader used to default
still defaults here. Only what a lab cannot run without is required.
"""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field

from ..brain.platform import (
    DEFAULT_NAN_HANDLING,
    DEFAULT_PASTEURIZATION,
    DEFAULT_TRUNCATION,
    TEST_PERIOD,
)
from ..schemas import Out

if TYPE_CHECKING:
    from ..db.models import Study


#: Studies with this sampler breed generations (labs.ga) instead of asking Optuna.
GA_SAMPLER = "ga"
#: Studies with this sampler write their own expressions (labs.search), asked define-by-run.
SEARCH_SAMPLER = "search"
TEMPLATE_SAMPLER = "template"
POWER_POOL_SAMPLER = "power-pool"
#: Studies that re-run one proven expression across markets and settings (tools.settings_sampler).
SETTINGS_SAMPLER = "settings-sampler"
#: Studies that re-shape one Alpha's expression at its own settings (tools.correlation_breaker).
CORRELATION_BREAKER = "correlation-breaker"
#: Studies that swap one Alpha's data fields for others, growing from what passes
#: (tools.fields_sampler).
FIELDS_SAMPLER = "fields-sampler"
#: Studies that combine your submitted Alphas into SuperAlphas (labs.super_alpha).
SUPER_LAB = "super-alpha"
#: Studies that are research-lab tasks, run only from the Tasks tab, by their lab's name.
TASK_SAMPLERS = {
    SEARCH_SAMPLER: "Search Lab",
    TEMPLATE_SAMPLER: "Template Lab",
    GA_SAMPLER: "Evolution Lab",
    POWER_POOL_SAMPLER: "LLM Power Pool Lab",
    SETTINGS_SAMPLER: "Settings Sampler",
    CORRELATION_BREAKER: "Correlation Breaker",
    FIELDS_SAMPLER: "Fields Sampler",
    SUPER_LAB: "Super Alpha Lab",
}


class TaskParams(Out):
    """Fields every research-lab task carries."""

    model_config = ConfigDict(extra="allow")

    region: str
    delay: int
    #: Concurrent slots the task holds; its rounds are ten simulations per core.
    cores: int = 1
    #: When it was handed to the scheduler, which starts waiting tasks oldest first.
    queued_at: str | None = None
    #: Stopped early: it only scores what is already out, then completes.
    stopping: bool = False

    def dump(self) -> dict[str, Any]:
        """The JSON stored in the column, in its stored spelling."""
        return self.model_dump(exclude_none=True)


class SearchParams(TaskParams):
    space: dict[str, Any]
    decay: int = 0
    dataset_ids: list[str] = Field(default_factory=list)
    n_startup_trials: int = 20


class TemplateParams(SearchParams):
    #: The template as typed, ``$variables`` and all (``labs.template``).
    template: str = ""
    truncation: float = DEFAULT_TRUNCATION
    pasteurization: str = DEFAULT_PASTEURIZATION
    nan_handling: str = DEFAULT_NAN_HANDLING
    test_period: str = TEST_PERIOD


class EvolutionParams(TaskParams):
    universe: str
    seeds: list[str] = Field(default_factory=list)
    population: int = 100
    mutation_rate: float | None = None
    test_period: str | None = None
    neutralizations: list[str] = Field(default_factory=list)


class PowerPoolParams(TaskParams):
    universes: list[str]
    neutralizations: list[str]
    universe: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    #: The Data Explorer's filter the datasets were chosen under, applied on every call.
    field_filter: dict[str, Any] | None = None
    model: str = ""
    #: Off, a call ``model`` cannot make (no budget left, provider down) goes to the next model
    #: a Key can run; on, the task waits for ``model`` alone.
    only_model: bool = False
    #: Running LLM tallies: ``calls``, ``empty``, ``failed``, ``byDataset`` and ``byModel``.
    llm: dict[str, Any] = Field(default_factory=dict)
    #: The last hundred LLM calls, for the task's detail view.
    calls: list[dict[str, Any]] = Field(default_factory=list)


class SettingsParams(TaskParams):
    """Settings Sampler: every simulation is written up front, so nothing is sampled.

    ``region`` and ``delay`` are the source Alpha's, shown on the task card; the task itself
    spans whichever markets were chosen.
    """

    alpha_id: str = ""
    #: How many region/delay/universe markets the sweep covers, for the task's detail line.
    markets: int = 0
    #: Held at the source Alpha's values for every simulation in the sweep.
    decay: int = 0
    truncation: float = DEFAULT_TRUNCATION
    #: Truncation set per market by the Truncation Agent instead of held at ``truncation``.
    truncation_agent: bool = False
    nan_handling: str = DEFAULT_NAN_HANDLING
    #: ``P{years}Y{months}M0D``. Empty on a task added before it was recorded.
    test_period: str = ""


class BreakerParams(TaskParams):
    """Correlation Breaker: one Alpha re-shaped, every simulation written up front.

    The settings are the source Alpha's and are never varied, so they are recorded here to be
    shown on the task card rather than to be chosen from.
    """

    alpha_id: str = ""
    universe: str = ""
    neutralization: str = ""
    decay: int = 0
    truncation: float = DEFAULT_TRUNCATION
    #: The recipes queued, by id, for the task's detail line.
    recipes: list[str] = Field(default_factory=list)


class FieldsSamplerParams(BreakerParams):
    """Fields Sampler: one Alpha's fields swapped, its settings the source Alpha's. Every
    winner is swept the same way, so ``mode`` and ``k`` are kept for those later sweeps."""

    mode: Literal["sweep", "similar", "buckets"] = "sweep"
    #: Similar: the most fields taken from each tier. Buckets: the picks from each bucket.
    k: int = 10
    #: The source Alpha's fields chosen to be swapped first.
    fields: list[str] = Field(default_factory=list)
    #: Other Alphas of the same expression whose settings every swap also runs at.
    alphas: list[str] = Field(default_factory=list)


class SuperParams(TaskParams):
    """Super Alpha Lab: every selection and combo pairing is written up front."""

    universe: str = ""
    neutralization: str = ""
    selection_limit: int = 30
    #: ``IS``, ``OS``, or both: each pairing then runs once under each.
    activation: list[str] = Field(default_factory=lambda: ["IS", "OS"])
    #: The recipes queued, as ``selection · combo``, for the task's detail line.
    recipes: list[str] = Field(default_factory=list)


BY_SAMPLER: dict[str, type[TaskParams]] = {
    SEARCH_SAMPLER: SearchParams,
    TEMPLATE_SAMPLER: TemplateParams,
    GA_SAMPLER: EvolutionParams,
    POWER_POOL_SAMPLER: PowerPoolParams,
    SETTINGS_SAMPLER: SettingsParams,
    CORRELATION_BREAKER: BreakerParams,
    FIELDS_SAMPLER: FieldsSamplerParams,
    SUPER_LAB: SuperParams,
}


def params_of[P: TaskParams](row: Study, kind: type[P]) -> P:
    """The row's stored parameters as ``kind``; raises if the row belongs to another lab."""
    expected = BY_SAMPLER.get(row.sampler)
    if expected is None or not issubclass(expected, kind):
        raise TypeError(f"A {row.sampler!r} task has no {kind.__name__}.")
    return kind.model_validate(row.sampler_params or {})


def task_params(row: Study) -> TaskParams:
    """The row's parameters as its own lab's model, whatever the lab."""
    return BY_SAMPLER[row.sampler].model_validate(row.sampler_params or {})
