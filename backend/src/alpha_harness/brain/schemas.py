"""Pydantic models of BRAIN API objects.

The wire format is camelCase; models accept either spelling and serialise back to camelCase.

Deliberately permissive — ``extra="allow"`` everywhere — because the platform adds fields
over time and unknown ones should survive into the UI rather than be silently dropped.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

import msgspec
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .platform import (
    DEFAULT_DECAY,
    DEFAULT_NAN_HANDLING,
    DEFAULT_NEUTRALIZATION,
    DEFAULT_PASTEURIZATION,
    DEFAULT_TRUNCATION,
    DEFAULT_UNIT_HANDLING,
    INSTRUMENT_TYPE,
    LANGUAGE,
    REGION_AGNOSTIC_REGION,
    TEST_PERIOD,
)


class BrainModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


# --- enumerations ---------------------------------------------------------
# Resolve the authoritative values at runtime from OPTIONS /simulations. These exist for
# ergonomics and for the few places a literal is genuinely fixed.


class SimulationType(StrEnum):
    """What a simulation is, and what the Alphas it makes are.

    ``REGION_AGNOSTIC`` is a request type only; it produces one ``RA_PARENT`` Alpha holding
    up to four ``RA_CHILD`` Alphas, one per region. Measured live: the simulation answers
    with the parent's id, and the parent carries ``children``.
    """

    REGULAR = "REGULAR"
    SUPER = "SUPER"
    REGION_AGNOSTIC = "REGION_AGNOSTIC"
    RA_PARENT = "RA_PARENT"
    RA_CHILD = "RA_CHILD"


#: The simulation mode whose Alphas BRAIN refuses to submit — and refuses even to check:
#: ``GET /alphas/{id}/check`` answers ``400 Cannot check submission for QUICK mode alphas``.
#:
#: Measured against the same expressions run both ways: every figure is identical, train and
#: test years included. What a quick Alpha does *not* get is the investability-constrained and
#: risk-neutralized blocks, the yearly-stats recordset, and twelve of the nineteen checks: it
#: keeps the seven that score it and loses correlation, Theme and Competition. It costs one
#: simulation and one concurrent core, like a full one, but can finish sooner: a GLB batch of
#: ten took 334 s against 478 s, sent side by side.
QUICK_MODE = "QUICK"
#: BRAIN's default mode: a request that names it is the same request as one that does not.
FULL_MODE = "FULL"


def produced_type(requested: str) -> str:
    """The Alpha type a request of ``requested`` comes back as.

    Only region-agnostic differs: it is asked for as ``REGION_AGNOSTIC`` and returns an
    ``RA_PARENT`` carrying its children, so the two never compare equal by name and anything
    matching a request against its result has to translate first.
    """
    if requested == SimulationType.REGION_AGNOSTIC:
        return SimulationType.RA_PARENT
    return requested


def region_label(region: str) -> str:
    """A region as a sentence says it. ``ALL`` on its own reads as a placeholder."""
    return "all regions" if region == REGION_AGNOSTIC_REGION else region


class SimulationStatus(StrEnum):
    """Every simulation ``status``.

    ``WAITING`` and ``SIMULATING`` are still running; the rest are final.
    """

    WAITING = "WAITING"
    SIMULATING = "SIMULATING"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    FAIL = "FAIL"


class CheckResult(StrEnum):
    PASS = "PASS"  # noqa: S105 - check outcome, not a password
    FAIL = "FAIL"
    PENDING = "PENDING"
    WARNING = "WARNING"
    ERROR = "ERROR"


# --- authentication -------------------------------------------------------


class TokenInfo(BrainModel):
    expiry: float | None = None


class AuthUser(BrainModel):
    id: str


class AuthState(BrainModel):
    """Response of ``GET``/``POST /authentication``."""

    user: AuthUser | None = None
    token: TokenInfo | None = None
    permissions: list[str] = Field(default_factory=list)

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user else None


# --- simulation -----------------------------------------------------------


class SimulationSettings(BrainModel):
    """The settings block of a simulation request.

    Only ``instrument_type``/``region``/``universe``/``delay`` are universally required;
    the rest carry platform defaults. Validate combinations against
    ``OPTIONS /simulations`` rather than trusting these types.
    """

    instrument_type: str = INSTRUMENT_TYPE
    region: str
    universe: str
    delay: int
    decay: int = DEFAULT_DECAY
    neutralization: str = DEFAULT_NEUTRALIZATION
    truncation: float = DEFAULT_TRUNCATION
    pasteurization: str = DEFAULT_PASTEURIZATION
    unit_handling: str = DEFAULT_UNIT_HANDLING
    #: Always ``ON`` in what this application sends; see :class:`SimulationRequest`.
    nan_handling: str = DEFAULT_NAN_HANDLING
    language: str = LANGUAGE
    visualization: bool = False
    test_period: str | None = None
    max_trade: str | None = None
    max_position: str | None = None
    #: ``FULL`` or ``QUICK``; BRAIN defaults it to ``FULL``. Left unset, the engine sends
    #: ``QUICK`` in the regions Settings name (see :data:`QUICK_MODE`).
    simulation_mode: str | None = None
    #: SUPER only. Which selection weights count (``POSITIVE`` is BRAIN's default), how many
    #: of the top-ranked Alphas are kept (at least 10), and whether each component counts
    #: from its in-sample start (``IS``) or only from its out-of-sample start (``OS``).
    selection_handling: str | None = None
    selection_limit: int | None = None
    component_activation: str | None = None


class SimulationRequest(BrainModel):
    """The body of ``POST /simulations``.

    Note the asymmetry with :class:`Alpha`: on submission the expression key is a bare
    string; on retrieval the same key expands into an object.
    """

    type: SimulationType = SimulationType.REGULAR
    settings: SimulationSettings
    regular: str | None = None
    combo: str | None = None
    selection: str | None = None

    @model_validator(mode="after")
    def _hold_out_test_period(self) -> Self:
        """Hold out a test period unless one was named.

        Enforced here because every request — from a lab, a template or the simulations
        API — becomes this model before it is hashed or sent. The held-out years let the
        Pool hide Alphas that collapse out of sample; BRAIN still runs the submission
        checks on the whole period.

        NaN handling is a plain default on :class:`SimulationSettings` rather than an
        override here: the Settings Sampler re-runs a source Alpha's own settings, and
        forcing it made the sweep disagree with the value its own screen showed.
        """
        if self.settings.test_period is None:
            self.settings = self.settings.model_copy(update={"test_period": TEST_PERIOD})
        return self

    @model_validator(mode="after")
    def _region_carries_the_type(self) -> Self:
        """Region ``ALL`` means a region-agnostic simulation; BRAIN offers it nowhere else.

        Deriving the type from the region rather than asking every lab to set it keeps one
        place to be wrong, and makes a market chosen in a form arrive here already correct.
        """
        if self.settings.region == REGION_AGNOSTIC_REGION:
            self.type = SimulationType.REGION_AGNOSTIC
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


# --- alpha ----------------------------------------------------------------


class Check(BrainModel):
    """One entry of the submission-check array.

    ``limit`` and ``value`` are usually a threshold and the figure measured against it, but
    that is a convention, not a contract. ``HT_ORTHOGONAL_RAM_NEUTRALIZATION`` puts
    *neutralization names* in both; ``HT_INVESTABLE_...`` puts a *list* of pool names in
    ``value``. Each new shape aborted the whole listing page it arrived on — the alphas are
    validated a page at a time, so one unknown check costs every alpha beside it. Untyped is
    the only width that cannot be outgrown again; readers test before they compute.
    """

    name: str
    result: CheckResult | None = None
    limit: Any = None
    value: Any = None
    # MATCHES_COMPETITION carries arrays instead of a numeric value.
    matched: list[Any] | None = None
    unmatched: list[Any] | None = None

    @property
    def numbers(self) -> tuple[float, float] | None:
        """``(value, limit)`` when both are real numbers, else ``None``."""
        if isinstance(self.value, float | int) and isinstance(self.limit, float | int):
            return float(self.value), float(self.limit)
        return None


class SampleStats(BrainModel):
    """The ``is`` / ``os`` / ``prod`` statistics block."""

    pnl: float | None = None
    book_size: float | None = None
    long_count: int | None = None
    short_count: int | None = None
    turnover: float | None = None
    returns: float | None = None
    drawdown: float | None = None
    margin: float | None = None
    fitness: float | None = None
    sharpe: float | None = None
    start_date: str | None = None
    #: On a submitted Alpha only: BRAIN's Production and Self-Correlation, as its Submitted
    #: Alphas page shows them.
    prod_correlation: float | None = None
    self_correlation: float | None = None
    checks: list[Check] = Field(default_factory=list)


class AlphaCode(BrainModel):
    """The expanded expression object returned on retrieval."""

    code: str | None = None
    description: str | None = None
    operator_count: int | None = None


class Alpha(BrainModel):
    """Response of ``GET /alphas/{id}``."""

    id: str
    type: SimulationType | None = None
    author: str | None = None
    settings: SimulationSettings | None = None
    regular: AlphaCode | None = None
    combo: AlphaCode | None = None
    selection: AlphaCode | None = None
    date_created: datetime | None = None
    date_submitted: datetime | None = None
    date_modified: datetime | None = None
    name: str | None = None
    #: Nullable on the wire, so a null must not fail the page.
    favorite: bool | None = None
    hidden: bool | None = None
    color: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    grade: str | None = None
    stage: str | None = None
    status: str | None = None
    #: Region-agnostic only: an ``RA_PARENT`` lists its per-region children here, and each
    #: ``RA_CHILD`` names the parent. Measured; the parent carries no statistics of its own.
    children: list[str] = Field(default_factory=list)
    parent: str | None = None
    # `is` is a Python keyword; the wire name is restored on serialisation.
    in_sample: SampleStats | None = Field(default=None, alias="is")
    os: SampleStats | None = None
    #: Undocumented: the years before a test period.
    train: SampleStats | None = None
    test: SampleStats | None = None
    prod: SampleStats | None = None

    @field_validator("tags", "children", mode="before")
    @classmethod
    def _drop_nulls(cls, value: Any) -> Any:
        """The codec allows a null tag, and a null ``children`` on anything not RA.

        Normalising both here keeps every reader on ``list[str]``.
        """
        if value is None:
            return []
        return [t for t in value if t is not None] if isinstance(value, list) else value

    @property
    def expression(self) -> str | None:
        for code in (self.regular, self.combo, self.selection):
            if code and code.code:
                return code.code
        return None


# --- recordsets -----------------------------------------------------------


class RecordProperty(BrainModel):
    name: str
    title: str | None = None
    type: str | None = None


class RecordSchema(BrainModel):
    name: str | None = None
    title: str | None = None
    properties: list[RecordProperty] = Field(default_factory=list)


class RecordSet(BrainModel):
    """A column-oriented time series.

    The wire format is a ``schema`` describing columns plus ``records`` as positional
    arrays — you must zip them yourself. :meth:`rows` does that.
    """

    schema_: RecordSchema = Field(default_factory=RecordSchema, alias="schema")
    records: list[list[Any]] = Field(default_factory=list)

    def rows(self) -> list[dict[str, Any]]:
        """Zip ``records`` against the column names.

        Rows shorter than the schema are padded with ``None`` rather than raising — a
        truncated series should still render.
        """
        names = [p.name for p in self.schema_.properties]
        if not names:
            return []
        out: list[dict[str, Any]] = []
        for record in self.records:
            padded = list(record) + [None] * (len(names) - len(record))
            out.append(dict(zip(names, padded, strict=False)))
        return out


# --- data catalog ---------------------------------------------------------


class DataCategoryRef(BrainModel):
    id: str
    name: str | None = None


class DataSet(BrainModel):
    """An entry of ``GET /data-sets``."""

    id: str
    name: str | None = None
    description: str | None = None
    category: DataCategoryRef | None = None
    subcategory: DataCategoryRef | None = None
    region: str | None = None
    delay: int | None = None
    universe: str | None = None
    coverage: float | None = None
    value_score: float | None = None
    user_count: int | None = None
    alpha_count: int | None = None
    field_count: int | None = None
    themes: list[Any] = Field(default_factory=list)
    pyramid_multiplier: float | None = None
    research_papers: list[Any] = Field(default_factory=list)


class FieldRef(msgspec.Struct):
    """A dataset, category or subcategory named inside a data field."""

    id: str | None = None
    name: str | None = None


class BulkField(msgspec.Struct, rename="camel"):
    """One entry of ``GET /data-fields``, decoded straight from the response bytes.

    A whole market is ~85k of these, where validating Pydantic models cost three times as
    much and blocked the event loop while it did. Only the columns the catalog stores are
    declared; msgspec skips the rest of each object.
    """

    #: Required: a field without one cannot be keyed, and a null key corrupts DuckDB's index.
    id: str
    description: str | None = None
    type: str | None = None
    coverage: float | None = None
    #: The share of the history that is actually populated, as against ``coverage``, which is
    #: the share of the instruments. A field can be complete on one and threadbare on the other.
    date_coverage: float | None = None
    user_count: int | None = None
    alpha_count: int | None = None
    pyramid_multiplier: float | None = None
    #: Untyped: plain names once, ``{id, name, multiplier}`` objects since. Stored as JSON
    #: and read by name, so either shape works and a third cannot fail a market's sync.
    themes: list[Any] | None = None
    #: When BRAIN first offered the field here. New fields are uncrowded by construction.
    date_created: date | None = None
    #: How many regions hold this field. Only region ``ALL`` sends it, and it is the one
    #: local signal of whether a field can survive a region-agnostic intersection.
    region_coverage: int | None = None
    dataset: FieldRef | None = None
    category: FieldRef | None = None
    subcategory: FieldRef | None = None


class BulkFields(msgspec.Struct):
    """The enveloped shape of ``GET /data-fields``, when it answers with one."""

    results: list[BulkField] = []


#: Reused: building the decoders once is most of their cost.
BULK_FIELDS = msgspec.json.Decoder(list[BulkField])
BULK_FIELDS_ENVELOPE = msgspec.json.Decoder(BulkFields)


class Operator(BrainModel):
    """An entry of ``GET /operators`` — the language reference."""

    name: str
    category: str | None = None
    scope: list[str] = Field(default_factory=list)
    definition: str | None = None
    description: str | None = None
    documentation: str | None = None
    level: str | None = None
