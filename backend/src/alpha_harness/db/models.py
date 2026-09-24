"""Operational state, stored in SQLite.

Everything here is small, transactional, and must survive a crash. The bulk analytical
data — the data-field catalog — lives in DuckDB instead (see :mod:`.duck`).

Schema evolution is additive only, applied by :func:`.sqlite.migrate` on every startup:
missing columns and indexes as well as missing tables, which ``create_all`` alone does
*not* add.

Never drop or rename a column on ``simulation_record``. Losing a row there means losing
the ability to cancel a running simulation, which is the exact failure this project
exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp stored as UTC and read back UTC-aware.

    SQLite has no zone: the stock type drops an aware value's offset without converting,
    so ``12:00-04:00`` came back as ``12:00``. Converting on the way in makes the stored
    text UTC; a naive value is refused rather than guessed at.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"Refusing to store naive datetime {value!r}; use utcnow().")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: UtcDateTime}


# --- credentials & session ------------------------------------------------


class Credential(Base):
    """A BRAIN login. The password is sealed by :class:`~alpha_harness.sealing.Sealer`."""

    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class BrainSessionRow(Base):
    """A cached cookie jar, so a restart does not cost another proof-of-work solve.

    Cookies are bearer credentials, so the jar is sealed just like the password.
    """

    __tablename__ = "brain_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("credential.id", ondelete="CASCADE"))
    cookies_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    user_id: Mapped[str | None] = mapped_column(String(64))
    permissions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


# --- simulations ----------------------------------------------------------


class SimStatus(StrEnum):
    """Local lifecycle. Distinct from the platform's own status.

    ``PENDING`` is the crash window: the request is in flight and the platform id is not
    known yet, so a row left there after a restart may be a real running simulation and
    must be reconciled rather than discarded.

    ``ORPHANED`` means it was sent but no id came back. Not final:
    :mod:`alpha_harness.engine.reconcile` adopts the alpha it produced or queues the
    request again, because treating it as final either loses the alpha or spends the
    quota twice.
    """

    QUEUED = "QUEUED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    ORPHANED = "ORPHANED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"

    @property
    def terminal(self) -> bool:
        return self not in ACTIVE


#: Not yet finished; every other status is final and no transition may leave it.
#: ``ORPHANED`` belongs here because an identical request arriving while its outcome is
#: reconciled must share the row rather than pay for a second run.
ACTIVE = (SimStatus.QUEUED, SimStatus.PENDING, SimStatus.RUNNING, SimStatus.ORPHANED)


class SimulationRecord(Base):
    """One simulation we asked BRAIN to run.

    The row is written **before** the HTTP request and updated with ``platform_id`` the
    moment a ``201`` lands, so a simulation can always be cancelled.

    For a multi-simulation, ``platform_id`` is the *parent* id — the only cancellable
    handle. Child ids only exist once the parent completes and are stored in
    ``child_ids``.
    """

    __tablename__ = "simulation_record"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Platform identity
    platform_id: Mapped[str | None] = mapped_column(String(64), index=True)
    alpha_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("simulation_record.id", ondelete="SET NULL")
    )
    child_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    is_batch: Mapped[bool] = mapped_column(default=False)
    #: Child ids exist only once the parent finishes, and resolving them costs one
    #: request each. This marks that work as done so it happens exactly once.
    children_expanded: Mapped[bool] = mapped_column(default=False)

    # What we asked for
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    expression: Mapped[str | None] = mapped_column(Text)

    # Batch-packing key: children of one multi-simulation must agree on all of these.
    sim_type: Mapped[str] = mapped_column(String(16), default="REGULAR")
    instrument_type: Mapped[str] = mapped_column(String(16), default="EQUITY")
    region: Mapped[str] = mapped_column(String(16))
    delay: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(16), default="FASTEXPR")
    universe: Mapped[str | None] = mapped_column(String(32))

    # Where it came from — lets the UI group work and apply per-task slot quotas.
    task: Mapped[str] = mapped_column(String(64), default="manual", index=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(16), default=SimStatus.PENDING, index=True)
    platform_status: Mapped[str | None] = mapped_column(String(16))
    progress: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)
    #: Times reconcile queued this row again after its send's answer was lost.
    orphan_requeues: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the POST that carried this row began — ``created_at`` is when it was queued,
    #: which can be hours earlier. Bounds the search for a send whose answer was lost.
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_polled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        Index("ix_sim_active", "status", "created_at"),
        Index("ix_sim_batch_key", "sim_type", "instrument_type", "region", "delay", "language"),
    )

    @property
    def elapsed_seconds(self) -> float | None:
        """Wall time since submission — what the matrix cell counts up."""
        if self.submitted_at is None:
            return None
        return ((self.finished_at or utcnow()) - self.submitted_at).total_seconds()


class DedupEntry(Base):
    """Hash of a canonical simulation payload -> the alpha it produced.

    Guards the daily quota against re-simulating something already run, as recommended
    in ``docs/wqb-documentation/brain-api/how-can-you-avoid-duplicate-simulations.md``.
    """

    __tablename__ = "dedup_entry"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    alpha_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class TaskQuota(Base):
    """How many of the concurrent slots a named task may hold at once.

    Research runs in parallel streams — a manual experiment, a template sweep, an
    optimizer study — and without a cap one of them starves the others. A task with no
    row here competes freely for whatever is unallocated.
    """

    __tablename__ = "task_quota"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_slots: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class QuotaSnapshot(Base):
    """A reading of ``X-RateLimit-*`` from a simulation POST.

    Kept as a series rather than a single row so the UI can show quota burn over the day.
    """

    __tablename__ = "quota_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    limit_total: Mapped[int | None] = mapped_column(Integer)
    remaining: Mapped[int | None] = mapped_column(Integer)
    reset_seconds: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class BrainCache(Base):
    """A BRAIN read kept so it is not asked for again: an Alpha's series, its correlations.

    Correlations and recordsets are Retry-After jobs, while an Alpha's in-sample history
    never changes until the period rolls. So the Alpha page reads from here and only goes
    back to BRAIN when asked to refresh.
    """

    __tablename__ = "brain_cache"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    body: Mapped[Any] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --- catalog sync ---------------------------------------------------------


class SyncStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SyncRun(Base):
    """One catalog crawl of a single (instrumentType, region, delay, universe) tuple."""

    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_type: Mapped[str] = mapped_column(String(16), default="EQUITY")
    region: Mapped[str] = mapped_column(String(16))
    delay: Mapped[int] = mapped_column(Integer)
    universe: Mapped[str] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), default=SyncStatus.RUNNING, index=True)
    phase: Mapped[str | None] = mapped_column(String(32))
    #: Unused. Retired once every install has run a build with these server defaults: a build
    #: without them cannot put the columns back, so the launcher could not fall back to it.
    cursor_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    truncated_datasets: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default="[]")

    datasets_synced: Mapped[int] = mapped_column(Integer, default=0)
    fields_synced: Mapped[int] = mapped_column(Integer, default=0)
    categories_synced: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (Index("ix_sync_tuple", "instrument_type", "region", "delay", "universe"),)


# --- platform metadata cache ---------------------------------------------


class MetadataCache(Base):
    """Cached platform metadata (``OPTIONS /simulations``, ``/operators``).

    Refreshed on login rather than hardcoded — regions and universes change.
    """

    __tablename__ = "metadata_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --- template studio (Phase 3) -------------------------------------------


class Template(Base):
    """A YAML alpha template with typed variables."""

    __tablename__ = "template"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(16), default="human")  # human | llm
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


# --- optimization (Phase 4) ----------------------------------------------


class StudyStatus(StrEnum):
    IDLE = "IDLE"
    #: Told to run, waiting for its cores to fit in the free slots.
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class TrialState(StrEnum):
    """Mirrors Optuna's states, plus the two this application adds.

    ``QUEUED`` and ``RUNNING`` both mean "asked for, not yet told" as far as Optuna is
    concerned; the distinction is whether a simulation has actually started, which is
    what the UI needs to show.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    PRUNED = "PRUNED"


class Study(Base):
    """One optimization run over one template.

    ``template_source`` is a *snapshot*, not a reference. A study is a record of a search
    over a particular space; if the template is edited afterwards the trials stop meaning
    anything, so the space is frozen at the moment the study is created.

    Optuna itself is rebuilt from the trial rows on demand rather than persisted
    separately. One source of truth, and the trials stay joinable to the simulations that
    produced them.
    """

    __tablename__ = "study"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("template.id", ondelete="SET NULL"))
    template_name: Mapped[str | None] = mapped_column(String(128))
    template_source: Mapped[str] = mapped_column(Text)

    sampler: Mapped[str] = mapped_column(String(32), default="nsga3")
    sampler_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    objectives: Mapped[list[Any]] = mapped_column(JSON, default=list)
    directions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    seed: Mapped[int | None] = mapped_column(Integer)

    #: Trials asked for per round. Kept a multiple of ten so a round fills whole
    #: multi-simulations rather than leaving slots holding part-empty batches.
    batch_size: Mapped[int] = mapped_column(Integer, default=10)
    max_trials: Mapped[int] = mapped_column(Integer, default=80)
    #: Slot-quota group, so a study cannot starve manual work.
    task: Mapped[str] = mapped_column(String(64), default="study")

    status: Mapped[str] = mapped_column(String(16), default=StudyStatus.IDLE, index=True)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)
    #: First time it began running. A task can wait as IDLE for days, so this is what "how
    #: long did it take" means; ``created_at`` would count the waiting too.
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    sampler_notes: Mapped[str] = mapped_column(Text, server_default="", nullable=False)


class Trial(Base):
    """One point in the search space, and what it turned into.

    The link to ``simulation_record`` is the whole reason trials live here rather than in
    Optuna's own storage: "which parameters produced this alpha" and "which simulation is
    this trial waiting on" are the two questions actually asked of a running study.
    """

    __tablename__ = "trial"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)

    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    distributions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expression: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    state: Mapped[str] = mapped_column(String(16), default=TrialState.QUEUED, index=True)
    values: Mapped[list[Any] | None] = mapped_column(JSON)
    #: Submission-check violations, keyed by the platform's own check name. Zero or
    #: less is feasible, following Optuna's convention.
    constraint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    feasible: Mapped[bool | None] = mapped_column()
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    message: Mapped[str | None] = mapped_column(Text)

    simulation_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("simulation_record.id", ondelete="SET NULL"), index=True
    )
    alpha_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Evolution Lab only: 0 for the seeds, then one per bred generation.
    generation: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        UniqueConstraint("study_id", "number", name="uq_trial_number"),
        Index("ix_trial_open", "study_id", "state"),
    )


# --- conversations --------------------------------------------------------


class ChatThread(Base):
    """One conversation with the assistant.

    Persisted because a consultant who closes the laptop mid-thought should not lose it,
    and because the scope is part of the conversation: "which fields" means nothing
    without knowing which market is being talked about.
    """

    __tablename__ = "chat_thread"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    instrument_type: Mapped[str] = mapped_column(String(16), default="EQUITY")
    region: Mapped[str] = mapped_column(String(16))
    delay: Mapped[int] = mapped_column(Integer)
    universe: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, index=True
    )


class ChatMessage(Base):
    """One turn. ``meta`` carries the picks, model and cost of an assistant turn."""

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("chat_thread.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ApiKey(Base):
    """An assistant key. Sealed at rest; only a masked hint is ever shown.

    ``provider`` says whose key it is. Rotation is per provider: a Groq key cannot answer
    a request for a Gemini model, and offering it would spend a retry to learn that.
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32), default="google", server_default="google")
    key_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    hint: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    #: The user's own daily request ceiling, overriding the model's when set. Required for a
    #: paid provider, which has no ceiling of its own, and optional for a free one.
    daily_limit: Mapped[int | None] = mapped_column(nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class KeyUsage(Base):
    """Local budget ledger per (key, model, quota day).

    Google exposes no remaining-quota endpoint, so RPM/TPM/RPD have to be tracked
    client-side or rotation is guesswork. The day is **America/Los_Angeles**, where AI
    Studio's quota clock lives; counting UTC days would hand a key's daily budget back
    hours early and produce 429s that look like the platform misbehaving.
    """

    __tablename__ = "key_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_key.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(64))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD, America/Los_Angeles
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    last_request_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (UniqueConstraint("api_key_id", "model", "day", name="uq_key_model_day"),)


class Submission(Base):
    """An Alpha the consultant has told us they submitted on BRAIN.

    Submission happens by hand on the platform, so nothing here can observe it. The row is
    the user's own record, and the Submission Planner treats it as a permanent pool member
    every later candidate has to clear.
    """

    __tablename__ = "submission"

    alpha_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
