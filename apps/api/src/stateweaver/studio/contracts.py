"""Authoritative, versioned contracts shared by persistence, HTTP and MCP."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def new_id() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(UTC).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class LayoutNode(Contract):
    x: float = Field(ge=-1000000, le=1000000)
    y: float = Field(ge=-1000000, le=1000000)
    width: float | None = Field(default=None, ge=100)
    height: float | None = Field(default=None, ge=80, le=1000000)


class WorkspaceLayout(Contract):
    version: int = Field(default=0, ge=0)
    nodes: dict[str, LayoutNode] = Field(default_factory=dict, max_length=4096)
    asset_order: dict[str, list[str]] = Field(default_factory=dict, max_length=100)


class WorkspaceLayoutUpdate(Contract):
    expected_version: int = Field(ge=0)
    nodes: dict[str, LayoutNode] = Field(default_factory=dict, max_length=4096)
    asset_order: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    reset: bool = False


Role = Literal[
    "screenwriter",
    "executive",
    "set_director",
    "visual_analyst",
    "task_analyst",
    "material_executor",
]
DirectorMode = Literal["current", "delegated-v1"]
ProductionLanguage = Literal["zh-CN", "en"]
ActorStatus = Literal["idle", "ready", "running", "waiting", "paused", "blocked", "complete"]
ProjectStatus = Literal["screenwriting", "preparing", "producing", "blocked", "complete", "failed"]
WorkflowVersion = Literal["set-v1"]
HarnessVersion = Literal["adaptive-v2"]


class ReviewSampling(Contract):
    """New-review defaults; serialized job sampling remains authoritative."""

    fps: float = Field(default=1, strict=True, gt=0, le=8)
    boundary_fps: float = Field(default=4, strict=True, gt=0, le=8)


class StudioConfig(Contract):
    production_language: ProductionLanguage = Field(
        default="en",
        exclude_if=lambda value: value == "en",
        frozen=True,
        description="Production language fixed at project creation. Defaults to English.",
    )
    production_lifecycle_policy: Literal["coherent-v1"] = "coherent-v1"
    full_context_policy: Literal["optional", "strict"] = Field(
        default="optional",
        exclude_if=lambda value: value == "optional",
        description=(
            "Full context pagination is optional by default, including existing projects. "
            "Strict explicitly requires complete task/phase reading. Fresh scoped read tokens, "
            "source dependencies and actual image inspection remain required."
        ),
    )
    review_sampling: ReviewSampling | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Video review sampling; unspecified new reviews use 1 fps / 4 boundary fps.",
    )
    text_model: str = Field(default="gt-6.1-sol-a", min_length=1)
    director_model: str | None = Field(
        default="gt-6.1-sol-a",
        min_length=1,
        description=(
            "Executive, Set directors and director delegates. "
            "Null preserves the shared text model on historical projects."
        ),
    )
    image_model: str = Field(default="gt-im-2.5-sun-a", min_length=1)
    video_model: str = Field(default="MiniMax-H3-Sol", min_length=1)
    video_mode: Literal["ref2va"] = "ref2va"
    repairs_per_clip: int = Field(default=1, ge=0)
    repairs_per_reference: int = Field(default=3, ge=0)
    executive_extra_attempts: int = Field(default=3, ge=0)
    video_executive_extra_attempts: int | None = Field(default=None, ge=0)
    continuation_seconds: float = Field(default=2, ge=2, le=15)
    no_progress_turns: int = Field(default=5, ge=1)
    reasoning_effort: str | None = None
    prepare_next_clip: bool = True
    latent_continuation_mode: Literal["off", "endpoint-anchor-v1"] = "endpoint-anchor-v1"
    director_continuation_choice: Literal[True] = True

    def model_for_role(self, role: Role) -> str:
        return self.text_model if role == "screenwriter" else self.director_model or self.text_model


class ImageInputBatch(Contract):
    id: str
    thread_id: str | None = None
    turn_id: str | None = None
    session_key: str
    media_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class ImageInputRecovery(Contract):
    id: str = Field(default_factory=new_id)
    scope_key: str
    batch_id: str = ""
    status: Literal["retrying", "recovered", "manual"] = "retrying"
    media_ids: list[str] = Field(default_factory=list)
    previous_thread_id: str | None = None
    previous_turn_id: str | None = None
    replacement_thread_id: str | None = None
    session_key: str
    reason: str
    created_at: str = Field(default_factory=now)
    completed_at: str | None = None


class ImageQuarantine(Contract):
    """Project-local input exclusion, keyed by original image SHA-256."""

    id: str
    media_id: str
    actor_id: str
    recovery_id: str
    reason: str
    created_at: str = Field(default_factory=now)


ExternalReviewIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
ExternalReviewText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


ExternalReviewPurpose = Literal[
    "content_safety",
    "reference_inspection",
    "reference_use",
    "generation_input",
    "candidate_evidence",
]


class ExternalReviewScope(Contract):
    """The exact production context in which an operator decision may be consumed."""

    actor_id: ExternalReviewIdentifier | None = None
    clip_id: ExternalReviewIdentifier | None = None
    candidate_id: ExternalReviewIdentifier | None = None
    purposes: list[ExternalReviewPurpose] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def coherent_scope(self) -> Self:
        if len(self.purposes) != len(set(self.purposes)):
            raise ValueError("外部审核用途不能重复")
        if not any((self.actor_id, self.clip_id, self.candidate_id)):
            raise ValueError("外部审核必须绑定角色、片段或候选")
        if "candidate_evidence" in self.purposes and self.candidate_id is None:
            raise ValueError("候选证据审核必须绑定候选")
        scoped_production = {
            "reference_inspection",
            "reference_use",
            "generation_input",
        }
        if set(self.purposes) & scoped_production and not (self.actor_id and self.clip_id):
            raise ValueError("参考审核与生成输入批准必须同时绑定角色和 Clip")
        if "candidate_evidence" in self.purposes and not (
            self.actor_id and self.clip_id and self.candidate_id
        ):
            raise ValueError("候选证据审核必须同时绑定角色、Clip 和候选")
        return self


class ExternalReviewItem(Contract):
    """One hash-bound observation; it never represents native model inspection."""

    media_id: ExternalReviewIdentifier
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["approved", "rejected"]
    observation: ExternalReviewText
    acknowledged_quarantine_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("acknowledged_quarantine_ids")
    @classmethod
    def unique_quarantines(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("已确认的隔离记录不能重复")
        return value


class ExternalMediaReview(Contract):
    """Append-only operator receipt, distinct from native StudioInspectionRow evidence."""

    id: str = Field(default_factory=new_id)
    project_id: ExternalReviewIdentifier
    reviewer_id: ExternalReviewIdentifier
    authorization_id: ExternalReviewIdentifier
    reason: ExternalReviewText
    scope: ExternalReviewScope
    items: list[ExternalReviewItem] = Field(min_length=1, max_length=128)
    created_at: str = Field(default_factory=now)
    revoked_at: str | None = None
    revocation_reason: str | None = None

    @model_validator(mode="after")
    def coherent_receipt(self) -> Self:
        media_ids = [item.media_id for item in self.items]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("同一外部审核收据不能重复包含素材")
        if bool(self.revoked_at) != bool(self.revocation_reason):
            raise ValueError("撤销时间和撤销原因必须同时存在")
        return self


class ReviewGap(Contract):
    media_id: str
    recovery_id: str
    reason: str
    kind: str = "image"
    timestamps: list[float] = Field(default_factory=list)


class ActorSession(Contract):
    """Independent native contexts owned by the same durable production actor."""

    key: str
    provider_binding: dict[str, Any] = Field(default_factory=dict)
    purpose: Literal[
        "planning", "candidate_review", "review_batch", "review_summary", "executive_production"
    ] = "planning"
    candidate_id: str | None = None
    batch_id: str | None = None
    thread_id: str | None = None
    history_turn_id: str | None = None
    context_epoch: int = Field(default=0, ge=0)
    image_receipts: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    image_count: int = Field(default=0, ge=0)
    viewed_media_ids: list[str] = Field(default_factory=list)
    planning_clip_id: str | None = None
    planning_round: str | None = None
    planning_kind: Literal["set", "clip", "lookahead"] | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    role_journal: dict[str, Any] = Field(default_factory=dict, exclude_if=lambda value: not value)
    context_events: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class PendingImageDelivery(Contract):
    """An image body returned by MCP but not yet acknowledged by a later model call."""

    id: str = Field(default_factory=new_id)
    media_id: str
    session_key: str
    actor_context_epoch: int = Field(ge=0)
    session_context_epoch: int = Field(ge=0)
    thread_id: str | None = None
    delivery_id: str
    receipt_key: str
    item_id: str
    created_at: str = Field(default_factory=now)


class RuntimeWaitTarget(Contract):
    kind: Literal[
        "job", "set", "clip", "candidate", "delegate_task", "visual_task", "proposal", "reference"
    ]
    id: str = Field(min_length=1)
    status: str


class RuntimeWait(Contract):
    kind: Literal["provider", "dependency", "review"]
    targets: list[RuntimeWaitTarget] = Field(min_length=1)
    wake_version: int = Field(ge=0)
    registered_at: str = Field(default_factory=now)
    registered_turn_id: str | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    reason: str = ""


class Actor(Contract):
    id: str = Field(default_factory=new_id)
    role: Role
    set_id: str | None = None
    parent_actor_id: str | None = None
    visual_task_id: str | None = None
    delegate_task_id: str | None = Field(default_factory=lambda: None)
    production_context_started_at: str | None = Field(default_factory=lambda: None)
    planning_context_started_at: str | None = Field(default_factory=lambda: None)
    status: ActorStatus = "ready"
    summary: str = "等待开始"
    thread_id: str | None = None
    turn_id: str | None = None
    history_turn_id: str | None = None
    context_epoch: int = 0
    goal: str = ""
    paused: bool = False
    wake_requested: bool = True
    wake_version: int = Field(default=0, ge=0)
    no_progress_count: int = 0
    progress_version: int = 0
    context_progress_turn_id: str | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    lease_owner: str | None = None
    lease_generation: int = 0
    lease_until: float = 0
    last_error: str | None = None
    production_blocker: dict[str, Any] | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    skill_hash: str | None = None
    status_since: str | None = None
    last_progress_at: str | None = None
    recovery_attempts: int = 0
    recovery_not_before: float = 0
    recovery_history: list[dict[str, Any]] = Field(default_factory=list)
    runtime_wait: RuntimeWait | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    progress_markers: list[str] = Field(default_factory=list)
    session_key: str = Field(default_factory=lambda: "planning")
    sessions: dict[str, ActorSession] = Field(default_factory=dict)
    last_image_input: ImageInputBatch | None = None
    image_input_recoveries: list[ImageInputRecovery] = Field(default_factory=list)
    pending_image_deliveries: list[PendingImageDelivery] = Field(default_factory=list)

    def request_wake(self) -> None:
        """Record a new notification even when a previous wake is still pending."""
        self.wake_version += 1
        self.wake_requested = True

    @property
    def native_session_blocked(self) -> bool:
        """Business notifications cannot repair an exhausted native session failure."""
        return self.status == "blocked" and bool(self.last_error)


class StoryFact(Contract):
    id: str = Field(default_factory=new_id)
    text: str
    kind: Literal["identity", "event", "ending", "dialogue", "detail"] = "detail"


StoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ScreenplayDuration(Contract):
    minimum_seconds: float = Field(gt=0, description="Whole-story natural playback estimate.")
    maximum_seconds: float = Field(gt=0, description="Upper bound of the whole-story estimate.")
    basis: StoryText = Field(
        description="Explain speech/action overlap, necessary reactions and any target shortfall."
    )

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.maximum_seconds < self.minimum_seconds:
            raise ValueError("整体预计时长的上限不能小于下限")
        return self


class ScreenplayCharacter(Contract):
    name: StoryText
    description: StoryText


class ScreenplayBeat(Contract):
    kind: Literal["action", "dialogue", "sound"]
    text: StoryText
    speaker: StoryText | None = None

    @model_validator(mode="after")
    def dialogue_speaker(self) -> Self:
        if self.kind == "dialogue" and not self.speaker:
            raise ValueError("对白必须注明说话者；旁白也应注明旁白身份")
        if self.kind != "dialogue" and self.speaker:
            raise ValueError("speaker 只用于对白，动作和声音的主体写在正文中")
        return self


class ScreenplayParagraph(Contract):
    title: StoryText = Field(description="Narrative heading, without Set/Clip IDs or timecodes.")
    beats: list[ScreenplayBeat] = Field(min_length=1)


class ScreenplayManuscript(Contract):
    title: StoryText
    logline: StoryText
    style: StoryText
    duration_estimate: ScreenplayDuration
    characters: list[ScreenplayCharacter]
    paragraphs: list[ScreenplayParagraph] = Field(
        min_length=1,
        description="Story paragraphs in reading order, not production Sets, Clips or time slots.",
    )


class ScreenplayDraft(Contract):
    manuscript: ScreenplayManuscript
    facts: list[StoryFact]


class Screenplay(Contract):
    format: Literal["story-v1"] = "story-v1"
    manuscript: ScreenplayManuscript | None = Field(default_factory=lambda: None)
    text: str = ""
    facts: list[StoryFact] = Field(default_factory=list)
    confirmed_at: str | None = None
    confirmed_hash: str | None = None
    frozen_text_sha256: str | None = Field(default_factory=lambda: None)


class StoryAnchor(Contract):
    event_id: str
    position: Literal["before", "after"] = "after"


class MemorySource(Contract):
    kind: Literal["screenplay_fact", "memory", "candidate", "job", "media", "user", "decision"]
    id: str
    note: str = ""


class MemoryScope(Contract):
    asset_ids: list[str] = Field(default_factory=list)
    set_ids: list[str] = Field(default_factory=list)
    clip_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    starts_at: StoryAnchor | None = None
    ends_at: StoryAnchor | None = None


class StoryAttribute(Contract):
    subject_id: str
    property: Literal["appearance", "position", "ownership", "relationship", "knowledge", "other"]
    value: str = Field(min_length=1)


class StoryEvent(Contract):
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    description: str = ""
    order: float
    story_clock: str | None = None
    source_fact_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    preconditions: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)
    results: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)
    narrative_mode: Literal["forward", "flashback", "replay", "time_jump"] = Field(
        default_factory=lambda: "forward", exclude_if=lambda value: value == "forward"
    )
    replay_of_event_id: str | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    completion_required: bool = Field(default_factory=lambda: True, exclude_if=lambda value: value)
    source: Literal["director", "legacy_evidence"] = "director"
    certainty: Literal["confirmed", "planned", "unknown"] = "planned"
    revision: int = 1
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class EventExecution(Contract):
    """A planned action interval, separate from observed evidence and media time."""

    event_id: str = Field(min_length=1)
    phase: Literal["begin", "continue", "complete", "replay"]
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    implementation: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Event execution must have a positive time interval")
        return self


class EventObservation(Contract):
    """A production verdict grounded in inspected evidence."""

    event_id: str = Field(min_length=1)
    phase: Literal["begin", "continue", "complete", "replay"]
    status: Literal["met", "missing", "uncertain"]
    reason: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    evidence_media_ids: list[str] = Field(min_length=1)
    verified_requirement_ids: list[str] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    actual_preconditions: dict[str, str] = Field(default_factory=dict)
    actual_results: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Event observation must have a positive time interval")
        return self


class SourceStateBoundary(Contract):
    """A source-authorized ellipsis for one exact execution boundary, not a new event."""

    prior_clip_id: str = Field(min_length=1)
    prior_event_id: str = Field(min_length=1)
    target_clip_id: str = Field(min_length=1)
    target_event_id: str = Field(min_length=1)
    state_keys: list[str] = Field(min_length=1)


class MemoryEntry(Contract):
    id: str = Field(default_factory=new_id)
    text: str
    kind: Literal["plan", "observation", "constraint"] = "plan"
    source_candidate_id: str | None = None
    evidence_media_ids: list[str] = Field(default_factory=list)
    approved_asset_id: str | None = None
    approved_by: str | None = None
    approval_reason: str | None = None
    source_job_id: str | None = None
    section: Literal[
        "screenplay",
        "events",
        "directives",
        "set_shared",
        "clip_plan",
        "clip_actual",
        "clip_continuation",
        "legacy",
        "log",
    ] = "legacy"
    basis: Literal["fact", "plan", "observed", "unknown", "provisional"] = "plan"
    status: Literal["effective", "superseded", "history"] = "effective"
    scope: MemoryScope = Field(default_factory=MemoryScope)
    attributes: list[StoryAttribute] = Field(default_factory=list)
    sources: list[MemorySource] = Field(default_factory=list)
    actor_id: str | None = None
    reason: str = ""
    record_key: str = ""
    state_boundary: SourceStateBoundary | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    version: int = 1
    supersedes_ids: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    approved_state_ids: list[str] = Field(default_factory=list)
    approved_attributes: list[str] = Field(default_factory=list)
    approval_purpose: str | None = None
    source_seconds: float | None = None
    normalization_note: str | None = None
    created_at: str = Field(default_factory=now)


class Memory(Contract):
    revision: int = 0
    story: list[MemoryEntry] = Field(default_factory=list)
    asset_state_ids: list[str] = Field(default_factory=list)


class Media(Contract):
    id: str = Field(default_factory=new_id)
    sha256: str
    mime_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    url: str
    owner_actor_id: str | None = Field(default_factory=lambda: None)
    source_candidate_id: str | None = Field(default_factory=lambda: None)
    source_revision_id: str | None = Field(default_factory=lambda: None)
    asset_id: str | None = Field(default_factory=lambda: None)


class SetPreviewSegment(Contract):
    clip_id: str
    candidate_id: str
    media_id: str
    title: str
    order: int
    duration: float | None = None


class SetPreview(Contract):
    """Derived playback of the current, contiguous selected Clip prefix."""

    set_id: str
    status: Literal["empty", "idle", "preparing", "ready", "failed"] = "empty"
    fingerprint: str | None = None
    selected_clip_count: int = 0
    total_clip_count: int = 0
    segments: list[SetPreviewSegment] = Field(default_factory=list)
    waiting_clip_id: str | None = None
    waiting_clip_title: str | None = None
    media: Media | None = None
    message: str = ""


class RepairPlan(Contract):
    defects: list[str] = Field(default_factory=list)
    preserve: list[str] = Field(default_factory=list)
    change: str = ""
    source_candidate_id: str | None = None
    source_revision_id: str | None = None
    mode: Literal["regenerate", "edit"] = "regenerate"


class RepairAttempt(Contract):
    """A durable reservation, preserved through technical retries and replanning."""

    id: str = Field(default_factory=new_id)
    kind: Literal["ordinary", "executive"]
    ordinal: int = Field(ge=1)
    actor_id: str
    plan: RepairPlan
    registered_at: str = Field(default_factory=now)
    job_ids: list[str] = Field(default_factory=list)
    reinforcement_revision_ids: list[str] = Field(default_factory=list)
    reinforcement_reason: str = ""
    cancelled_at: str | None = Field(default_factory=lambda: None)
    cancellation_reason: str = Field(default_factory=str)


class QualityCounts(Contract):
    generations: int = 0
    reviews: int = 0


class QualityBlocker(Contract):
    code: str
    reason: str
    owner_actor_id: str | None = None
    related_ids: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class QualityProgress(Contract):
    initial: QualityCounts = Field(default_factory=QualityCounts)
    ordinary: QualityCounts = Field(default_factory=QualityCounts)
    executive: QualityCounts = Field(default_factory=QualityCounts)
    ordinary_limit: int
    ordinary_registered: int
    ordinary_remaining: int
    # None is effective source-authorized unlimited admission, not a saved episode change.
    extra_limit: int | None
    recorded_extra_limit: int | None = None
    extra_approved: int
    extra_used: int
    extra_approved_unused: int
    extra_grantable: int | None
    pending_repairs: list[RepairAttempt] = Field(default_factory=list)
    allowed_operations: list[str] = Field(default_factory=list)
    blockers: list[QualityBlocker] = Field(default_factory=list)
    responsible_actor_id: str | None = None


class QualityDecision(Contract):
    actor_id: str
    action: Literal["escalate", "retry", "select", "compromise"]
    reason: str = Field(min_length=1)
    candidate_id: str | None = None
    reference_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    deviations: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class QualityEpisode(Contract):
    id: str = Field(default_factory=new_id)
    status: Literal["ordinary", "escalated", "intervening", "resolved"] = "ordinary"
    ordinary_attempts: int = Field(default=0, ge=0)
    extra_limit: int = Field(default=3, ge=0)
    extra_granted: int = Field(default=0, ge=0)
    extra_used: int = Field(default=0, ge=0)
    fallback_id: str | None = None
    plan: RepairPlan | None = None
    decisions: list[QualityDecision] = Field(default_factory=list)


class SelectionRecord(Contract):
    kind: Literal["quality_passed", "director_compromise", "user_selection", "system_fallback"]
    actor_id: str
    reason: str = Field(min_length=1)
    deviations: list[str] = Field(default_factory=list)
    delegated_task_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class ReferenceRegionApproval(Contract):
    id: str = Field(default_factory=new_id)
    actor_id: str
    clip_id: str
    media_id: str
    sha256: str
    crop: tuple[float, float, float, float]
    purpose: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    created_at: str = Field(default_factory=now)


class ReferenceCoverageSource(Contract):
    state_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    media_id: str = Field(min_length=1)
    sha256: str = Field(min_length=1)


class ReferenceCoverageApproval(Contract):
    id: str = Field(default_factory=new_id)
    actor_id: str
    clip_id: str
    media_id: str
    sha256: str
    source_bindings: list[ReferenceCoverageSource] = Field(min_length=1)
    evidence_media_ids: list[str] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class ReferenceRevision(Contract):
    id: str = Field(default_factory=new_id)
    state_id: str
    parent_id: str | None = None
    source_revision_ids: list[str] = Field(default_factory=list)
    view: str
    prompt: str
    media_id: str | None = None
    status: Literal["pending", "candidate", "selected", "rejected", "failed"] = "pending"
    reason: str = ""
    job_id: str | None = None
    created_at: str = Field(default_factory=now)
    source_media_ids: list[str] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    repair_plan: RepairPlan | None = None
    selection: SelectionRecord | None = None
    review_history: list[dict[str, Any]] = Field(default_factory=list)
    region_approvals: list[ReferenceRegionApproval] = Field(default_factory=list)
    coverage_approvals: list[ReferenceCoverageApproval] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class StatePreparationPlan(Contract):
    """A design image is not an observation of an already produced event."""

    mode: Literal["independent", "observed", "transition_target"]
    reason: str = Field(min_length=1)
    source_set_id: str | None = Field(default_factory=lambda: None)
    source_event_id: str | None = Field(default_factory=lambda: None)
    required_attributes: list[str] = Field(default_factory=list)


class StateEvolutionEvidence(Contract):
    kind: Literal["state", "event", "memory", "screenplay"]
    id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class StateEvolutionInput(Contract):
    kind: Literal["root", "derived", "unresolved"]
    parent_state_id: str | None = None
    reason: str = Field(min_length=1)
    evidence: list[StateEvolutionEvidence] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def explicit_parent(self) -> Self:
        if (self.kind == "derived") != bool(self.parent_state_id):
            raise ValueError("派生状态必须有且仅有一个主要父状态；根状态和待确认状态不能连父状态")
        if self.kind != "unresolved" and not self.evidence:
            raise ValueError("确认状态演变需要引用已记录的剧情或状态依据")
        if not self.reason.strip():
            raise ValueError("状态演变原因不能为空")
        return self


class StateEvolution(StateEvolutionInput):
    revision: int = Field(ge=1)
    actor_id: str
    created_at: str = Field(default_factory=now)
    evidence_hashes: dict[str, str] = Field(default_factory=dict)
    review_session_id: str | None = None


ReferencePurposeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StateReferencePurpose(Contract):
    """A concrete generation use; a narrative change alone is not an asset state."""

    purpose: ReferencePurposeText
    set_ids: list[ReferencePurposeText] = Field(default_factory=list, max_length=128)
    clip_ids: list[ReferencePurposeText] = Field(default_factory=list, max_length=128)
    required_materials: list[ReferencePurposeText] = Field(min_length=1, max_length=32)
    prepare_when: Literal["now", "before_generation", "after_source_selected"]

    @model_validator(mode="after")
    def has_target(self) -> Self:
        if not self.set_ids and not self.clip_ids:
            raise ValueError("参考用途必须关联需要它的片场或 Clip；纯剧情过程应写入剧情记忆")
        if len(set(self.set_ids)) != len(self.set_ids) or len(set(self.clip_ids)) != len(
            self.clip_ids
        ):
            raise ValueError("参考用途的片场或 Clip 不能重复")
        return self


class AssetState(Contract):
    id: str = Field(default_factory=new_id)
    asset_id: str
    label: str
    story_time: float
    description: str
    selected_revisions: dict[str, str] = Field(default_factory=dict)
    revision_ids: list[str] = Field(default_factory=list)
    is_base: bool = False
    repair_episodes: dict[str, QualityEpisode] = Field(default_factory=dict)
    starts_at: StoryAnchor | None = None
    ends_at: StoryAnchor | None = None
    attributes: list[StoryAttribute] = Field(default_factory=list)
    preparation: StatePreparationPlan | None = Field(default_factory=lambda: None)
    preparation_revision: int = Field(default_factory=lambda: 0)
    preparation_history: list[dict[str, Any]] = Field(default_factory=list)
    evolution: StateEvolution | None = Field(default_factory=lambda: None)
    evolution_history: list[StateEvolution] = Field(default_factory=list)
    reference_purpose: StateReferencePurpose | None = Field(default_factory=lambda: None)
    excluded_event_ids: list[ReferencePurposeText] = Field(
        default_factory=list, max_length=128, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def unique_excluded_events(self) -> Self:
        if len(self.excluded_event_ids) != len(set(self.excluded_event_ids)):
            raise ValueError("状态排除事件不能重复")
        return self


class VoiceReference(Contract):
    id: str = Field(default_factory=new_id)
    source_candidate_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    reason: str = Field(min_length=1)
    media_id: str | None = None
    job_id: str | None = None
    status: Literal["preparing", "selected", "historical", "failed"] = "preparing"
    created_at: str = Field(default_factory=now)


class LogicalAsset(Contract):
    id: str = Field(default_factory=new_id)
    name: str
    kind: str
    description: str
    required_views: list[str] = Field(default_factory=list)
    state_ids: list[str] = Field(default_factory=list)
    voice_bootstrap_clip_id: str | None = None
    selected_voice_id: str | None = None
    voice_references: list[VoiceReference] = Field(default_factory=list)
    purpose: Literal["story", "action", "layout", "target_frame", "auxiliary"] = "story"
    published_scope: MemoryScope = Field(default_factory=MemoryScope)
    publication_reason: str = ""


class ReferenceBinding(Contract):
    asset_id: str
    state_id: str
    revision_id: str


class VideoReferenceInput(ReferenceBinding):
    prepared_reference_id: str | None = None
    purpose: str = ""
    full_board_reason: str | None = None
    state_role: Literal["start", "target", "guidance"] = "start"


class ReferenceImageInstructions(Contract):
    change: str = ""
    preserve: list[str] = Field(default_factory=list)
    composition: str = ""
    reference_roles: dict[str, str] = Field(default_factory=dict)
    orientation: Literal["portrait", "landscape", "square"] | None = None


class PrepareReferenceInput(Contract):
    model_config = ConfigDict(str_strip_whitespace=True)

    revision_id: str
    clip_id: str | None = None
    purpose: str = Field(min_length=1)
    preferred_view: str | None = None
    crop: tuple[float, float, float, float] | None = None
    full_board_reason: str | None = None
    region_approval_id: str | None = None


class PrepareMediaInput(Contract):
    source_candidate_id: str
    kind: Literal["video", "audio", "image"]
    start_seconds: float = Field(ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    purpose: str = Field(min_length=1)

    @model_validator(mode="after")
    def interval(self) -> PrepareMediaInput:
        if self.kind != "image" and (
            self.end_seconds is None or not 2 <= self.end_seconds - self.start_seconds <= 15
        ):
            raise ValueError("video/audio excerpts must last 2–15 seconds")
        return self


class TimedMediaBinding(Contract):
    preparation_job_id: str | None = None
    voice_reference_id: str | None = None
    asset_id: str | None = None
    source_candidate_id: str | None = None
    purpose: str = Field(min_length=1)
    role: Literal["continuation", "edit_source", "motion", "identity", "voice", "ambience", "sound"]
    visual_state_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_source(self) -> TimedMediaBinding:
        if (
            sum(
                bool(x)
                for x in (
                    self.preparation_job_id,
                    self.voice_reference_id,
                    self.source_candidate_id,
                )
            )
            != 1
        ):
            raise ValueError("Choose one prepared excerpt, fixed voice version or full edit source")
        if self.source_candidate_id and self.role != "edit_source":
            raise ValueError("A complete candidate may only be an explicit edit_source")
        return self


class ClipAssetPlan(Contract):
    asset_id: str
    start_state_id: str
    end_state_id: str | None = None
    required_views: list[str] = Field(default_factory=list)


class ClipPreparation(Contract):
    """Editable speculation: never a source of submitted generation inputs."""

    id: str = Field(default_factory=new_id)
    revision: int = Field(default=1, ge=1)
    parent_id: str | None = None
    enabled: bool = True
    user_disabled: bool = False
    status: Literal["draft", "stale", "consumed", "disabled"] = "draft"
    prompt_draft: str = ""
    action_notes: str = ""
    bindings: list[VideoReferenceInput] = Field(default_factory=list)
    audio_references: list[TimedMediaBinding] = Field(default_factory=list)
    pending_requirements: list[str] = Field(default_factory=list)
    context_fingerprint: str = ""
    context_dependencies: list[dict[str, Any]] = Field(default_factory=list)
    source_fingerprint: str = ""
    invalidation_reasons: list[str] = Field(default_factory=list)
    reason: str = ""
    actor_id: str
    stable_snapshot: dict[str, Any] = Field(default_factory=dict)
    continuity_snapshot: dict[str, Any] = Field(default_factory=dict)
    stable_status: Literal["ready", "stale"] = "ready"
    continuity_status: Literal["ready", "waiting", "stale"] = "waiting"
    dependency_changes: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class ReferenceConflict(Contract):
    id: str = Field(default_factory=new_id)
    description: str = Field(min_length=1)
    conflicting_media_ids: list[str] = Field(min_length=1)
    status: Literal["open", "resolved", "withdrawn", "not_applicable"] = "open"
    resolution_method: Literal["crop", "replace", "revise"] | None = None
    replacement_media_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    correction_reason: str = ""
    evidence_media_ids: list[str] = Field(default_factory=list)

    # Retire applicability to the current recipe, never retract the historical observation.
    applicability_reason: str = ""
    current_evidence_media_ids: list[str] = Field(default_factory=list)
    retained_compatible_media_ids: list[str] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class ReferencePlan(Contract):
    id: str = Field(default_factory=new_id)
    clip_id: str
    actor_id: str
    revision: int = Field(default=1, ge=1)
    parent_id: str | None = None
    status: Literal["draft", "finalized", "needs_review", "submitted", "superseded", "legacy"] = (
        "draft"
    )
    bindings: list[VideoReferenceInput] = Field(default_factory=list)
    video_references: list[TimedMediaBinding] = Field(default_factory=list)
    audio_references: list[TimedMediaBinding] = Field(default_factory=list)
    reference_inputs: list[dict[str, Any]] = Field(default_factory=list)
    video_inputs: list[dict[str, Any]] = Field(default_factory=list)
    audio_inputs: list[dict[str, Any]] = Field(default_factory=list)
    previous_candidate_id: str | None = None
    previous_end_state: EndState | None = None
    inspection_gaps: list[ReviewGap] = Field(default_factory=list)
    conflicts: list[ReferenceConflict] = Field(default_factory=list)
    conflict_check: str = ""
    prompt: str = ""
    prompt_reconciliation: str = ""
    context_fingerprint: str = ""
    dependency_fingerprint: str = ""
    context_dependencies: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    invalidation_reasons: list[str] = Field(default_factory=list)
    creative_fingerprint: str = ""
    affected_media_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    job_id: str | None = None
    created_at: str = Field(default_factory=now)
    finalized_at: str | None = None
    preparation_id: str | None = Field(default_factory=lambda: None)
    automatic_draft: bool = False
    preparation_revision: int | None = None
    continuity_check: dict[str, str] = Field(default_factory=dict)
    use_previous_clip_reference: bool | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    continuation_decision: dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )


ContextPhase = Literal["planning", "references", "generation", "review"]


class ContextRead(Contract):
    id: str
    actor_id: str
    target_id: str
    phase: ContextPhase
    fingerprint: str
    next_page: int = 0
    page_count: int = 1
    complete: bool = False
    protocol_version: int = 1
    session_key: str = ""
    snapshot_id: str | None = None
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    core_fingerprint: str = ""
    mode: Literal["full", "delta"] = "full"
    baseline_fingerprint: str | None = None
    read_kind: Literal["mandatory", "tool"] = "mandatory"
    wake_fingerprint: str | None = None
    created_at: str = Field(default_factory=now)


class RequiredContextPage(Contract):
    context_id: str
    phase: ContextPhase
    target_id: str
    fingerprint: str
    page: int
    page_count: int
    content: str
    next_page: int | None = None
    context_token: str | None = None
    revision: int
    mode: Literal["full", "delta"] = "full"
    baseline_fingerprint: str | None = None
    record_count: int = 0
    snapshot_id: str | None = None
    protocol_version: int = 2
    baseline_context_id: str | None = None
    read_reason: str = ""
    payload_bytes: int = 0
    full_payload_bytes: int = 0


class EndState(Contract):
    frame_media_id: str
    action: str
    space: str
    camera: str
    identity: str


class EndStateConfirmation(Contract):
    """A director's inspected end state; the model's original remains on Candidate."""

    end_state: EndState
    actor_id: str
    reason: str = Field(min_length=1)
    evidence_media_ids: list[str] = Field(min_length=1)
    visual_state_ids: list[str] = Field(default_factory=list)
    analysis_request_id: str | None = None
    coverage_gaps: list[ReviewGap] = Field(default_factory=list)
    delegated_task_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class VisualFinding(Contract):
    description: str = Field(min_length=1)
    severity: Literal["minor", "repair"]
    evidence_media_ids: list[str] = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    category: Literal["identity", "state", "action", "space", "camera", "other"] = "other"


class ReviewFindingAssessment(Contract):
    batch_id: str
    finding_index: int = Field(ge=0)
    conclusion: Literal["confirmed", "dismissed"]
    reason: str = Field(min_length=1)
    evidence_media_ids: list[str] = Field(min_length=1)


class RepairGuidance(Contract):
    finding_index: int = Field(ge=0)
    suggested_change: str = Field(min_length=1)
    preserve: list[str] = Field(min_length=1)
    material_gap: str | None = None
    next_check: str = Field(min_length=1)


class CandidateSelectionAssessment(Contract):
    story_coverage: Literal["met", "minor_deviation", "major_deviation", "unknown"]
    identity_consistency: Literal["met", "minor_deviation", "major_deviation", "unknown"]
    visual_quality: Literal["met", "minor_deviation", "major_deviation", "unknown"]
    reason: str = Field(min_length=1)
    evidence_media_ids: list[str] = Field(min_length=1)


class CandidateReview(Contract):
    actor_id: str
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1)
    mode: Literal["director", "external_operator"] = "director"
    external_review_ids: list[str] = Field(default_factory=list)
    external_reviewer_id: str | None = None
    external_authorization_id: str | None = None
    findings: list[VisualFinding] = Field(default_factory=list)
    evidence_media_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    batch_assessments: list[ReviewFindingAssessment] = Field(default_factory=list)
    coverage_gaps: list[ReviewGap] = Field(default_factory=list)
    selection_assessment: CandidateSelectionAssessment | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    event_observations: list[EventObservation] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    created_at: str = Field(default_factory=now)

    @model_validator(mode="after")
    def coherent_external_provenance(self) -> Self:
        provenance = bool(
            self.external_review_ids or self.external_reviewer_id or self.external_authorization_id
        )
        if self.mode == "external_operator":
            if not (
                self.external_review_ids
                and self.external_reviewer_id
                and self.external_authorization_id
            ):
                raise ValueError("外部候选审核必须保存完整审核来源")
        elif provenance:
            raise ValueError("原生候选审核不能携带外部审核来源")
        if len(self.external_review_ids) != len(set(self.external_review_ids)):
            raise ValueError("外部审核收据不能重复")
        return self


class ReviewBatch(Contract):
    id: str = Field(default_factory=new_id)
    analysis_job_id: str
    media_ids: list[str] = Field(min_length=1, max_length=6)
    purpose: Literal["footage", "references", "followup"] = "footage"
    question: str = ""
    status: Literal["pending", "complete"] = "pending"
    actor_id: str | None = None
    reason: str = ""
    findings: list[VisualFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    completed_at: str | None = None
    context_attempt: int = Field(default=0, ge=0)


class CandidateDisposition(Contract):
    kind: Literal["technical_failure", "cancelled", "superseded"]
    reason: str = Field(min_length=1)
    job_ids: list[str] = Field(default_factory=list)
    actor_id: str | None = None
    outcome: Literal["failed", "unknown"] = "failed"
    created_at: str = Field(default_factory=now)


class Candidate(Contract):
    id: str = Field(default_factory=new_id)
    clip_id: str
    job_id: str
    prompt: str
    bindings: list[ReferenceBinding]
    previous_candidate_id: str | None = None
    media_id: str | None = None
    end_state: EndState | None = None
    confirmed_end_state: EndStateConfirmation | None = None
    end_state_history: list[EndStateConfirmation] = Field(default_factory=list)
    analysis: dict[str, Any] = Field(default_factory=dict)
    review: CandidateReview | None = None
    disposition: CandidateDisposition | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    status: Literal["generating", "review", "accepted", "rejected", "failed"] = "generating"
    issues: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    repair_plan: RepairPlan | None = None
    selection: SelectionRecord | None = None
    review_history: list[CandidateReview] = Field(default_factory=list)
    observed_asset_states: dict[str, str] = Field(default_factory=dict)
    continuation_reference_job_id: str | None = Field(default_factory=lambda: None)
    review_batches: list[ReviewBatch] = Field(default_factory=list)
    review_gaps: list[ReviewGap] = Field(default_factory=list)


class ProductionFailure(Contract):
    category: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    job_ids: list[str] = Field(default_factory=list)
    upstream_clip_ids: list[str] = Field(default_factory=list)
    outcome: Literal["failed", "unknown"] = "failed"
    created_at: str = Field(default_factory=now)


class Clip(Contract):
    id: str = Field(default_factory=new_id)
    set_id: str
    order: int = Field(ge=0)
    title: str
    description: str
    duration: float = Field(default=5, ge=5, le=15.083333333333334)
    status: Literal[
        "planned", "generating", "review", "accepted", "blocked", "escalated", "repair", "failed"
    ] = "planned"
    candidate_ids: list[str] = Field(default_factory=list)
    accepted_candidate_id: str | None = None
    terminal_failure: ProductionFailure | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    repair_count: int = 0
    repair_reason: str | None = None
    archived_at: str | None = None
    plan_parent_ids: list[str] = Field(default_factory=list)
    story_time: float | None = None
    story_end_time: float | None = None
    asset_plan: list[ClipAssetPlan] = Field(default_factory=list)
    speaking_asset_ids: list[str] = Field(default_factory=list)
    continuity: dict[str, str] = Field(default_factory=dict)
    repair_plan: RepairPlan | None = None
    quality_episode: QualityEpisode | None = None
    pending_attempt_kind: Literal["initial", "ordinary", "executive", "technical"] = "initial"
    story_event_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    event_execution: list[EventExecution] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    starts_at: StoryAnchor | None = None
    ends_at: StoryAnchor | None = None
    plan_sources: list[MemorySource] = Field(default_factory=list)
    state_notes: list[str] = Field(default_factory=list)
    state_readiness: Literal["ready", "needs_verification"] = "ready"
    reference_plan_id: str | None = None
    preparation: ClipPreparation | None = Field(default_factory=lambda: None)
    preparation_history: list[ClipPreparation] = Field(default_factory=list)
    repair_attempts: list[RepairAttempt] = Field(default_factory=list)
    pending_repair_id: str | None = None
    quality_progress: QualityProgress | None = None
    # Explicit maintenance reconciliation; immutable submitted requests stay intact.
    quality_accounting: dict[str, Literal["initial", "ordinary", "executive"]] = Field(
        default_factory=dict
    )
    quality_accounting_history: list[dict[str, Any]] = Field(default_factory=list)
    plan_change_history: list[dict[str, Any]] = Field(default_factory=list)


class AssetHandoffPlan(Contract):
    id: str = Field(default_factory=new_id)
    asset_id: str
    mode: Literal["independent", "shared", "observed"]
    reason: str = Field(min_length=1)
    state_ids: list[str] = Field(default_factory=list)
    source_set_id: str | None = Field(default_factory=lambda: None)
    source_event_id: str | None = Field(default_factory=lambda: None)
    target_event_ids: list[str] = Field(default_factory=list)
    required_attributes: list[str] = Field(default_factory=list)


class HandoffConfirmation(Contract):
    actor_id: str
    reason: str
    observation_ids: list[str]
    reference_ids: list[str]
    source_fingerprint: str
    evidence: list[dict[str, Any]]
    reference_fingerprint: str
    created_at: str = Field(default_factory=now)


class AssetHandoff(AssetHandoffPlan):
    confirmation: HandoffConfirmation | None = Field(default_factory=lambda: None)


class SetDurationBudget(Contract):
    """Executive allocation in playback seconds, independent of story time and ETA."""

    target_seconds: float = Field(gt=0)
    minimum_seconds: float = Field(gt=0)
    maximum_seconds: float = Field(gt=0)
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.minimum_seconds <= self.target_seconds <= self.maximum_seconds:
            raise ValueError("Set 时长需满足 minimum_seconds ≤ target_seconds ≤ maximum_seconds")
        if not self.basis.strip():
            raise ValueError("Set 时长预算需说明自然节奏依据")
        return self


class SetDurationAdjustment(Contract):
    id: str = Field(default_factory=new_id)
    status: Literal["pending", "resolved"] = "pending"
    fingerprint: str
    planned_seconds: float
    plan_complete: bool
    budget_revision: int
    reason: str
    owner_id: str
    created_by: str
    created_at: str = Field(default_factory=now)
    resolved_at: str | None = None


class PlaybackSetStatus(Contract):
    set_id: str
    title: str
    budget: SetDurationBudget | None = Field(default_factory=lambda: None)
    budget_revision: int = Field(default_factory=lambda: 0)
    plan_complete: bool
    planned_clips: int
    accounted_seconds: float
    selected_actual_seconds: float
    selected_missing_duration: int = Field(default_factory=lambda: 0)
    status: Literal[
        "unplanned",
        "incomplete",
        "within_budget",
        "needs_budget",
        "below_minimum",
        "above_maximum",
        "display_only",
    ]
    can_submit: bool
    reason: str = ""
    owner_id: str | None = Field(default_factory=lambda: None)
    adjustment_id: str | None = Field(default_factory=lambda: None)


class PlaybackDuration(Contract):
    policy: Literal["display-only", "set-budget-v1"]
    estimate: ScreenplayDuration | None = None
    planned_seconds: float
    selected_actual_seconds: float
    selected_missing_duration: int = Field(default_factory=lambda: 0)
    final_actual_seconds: float | None = Field(default_factory=lambda: None)
    plan_complete: bool
    unplanned_set_ids: list[str] = Field(default_factory=list)
    sets: list[PlaybackSetStatus] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProductionSet(Contract):
    """Stable historical storage identity; set-v1 exposes this unit as a Set."""

    id: str = Field(default_factory=new_id)
    title: str
    description: str
    order: int = Field(ge=0)
    story_time: float
    depends_on: list[str] = Field(default_factory=list)
    actor_id: str
    memory: Memory = Field(default_factory=Memory)
    clip_ids: list[str] = Field(default_factory=list)
    replan_repair_count: int = 0
    replan_parent_ids: list[str] = Field(default_factory=list)
    status: Literal["planned", "producing", "blocked", "complete", "failed"] = "planned"
    story_goal: str = Field(default_factory=str)
    entry_state: str = Field(default_factory=str)
    exit_state: str = Field(default_factory=str)
    asset_ids: list[str] = Field(default_factory=list)
    story_event_ids: list[str] = Field(default_factory=list)
    planning_ready_at: str | None = Field(default_factory=lambda: None)
    handoff_history: list[dict[str, Any]] = Field(default_factory=list)
    asset_handoffs: list[AssetHandoff] = Field(default_factory=list)
    dependency_revision: int = Field(default_factory=lambda: 0)
    dependency_history: list[dict[str, Any]] = Field(default_factory=list)
    duration_budget: SetDurationBudget | None = Field(default_factory=lambda: None)
    duration_budget_revision: int = Field(default_factory=lambda: 0, ge=0)
    duration_adjustment: SetDurationAdjustment | None = Field(default_factory=lambda: None)
    duration_history: list[dict[str, Any]] = Field(default_factory=list)
    clip_plan_complete: bool = Field(default_factory=lambda: False)


class ProposalSourceWait(Contract):
    source_set_id: str
    source_event_id: str


class ProposalRequirement(Contract):
    id: str = Field(default_factory=new_id)
    kind: Literal["memory", "state", "reference", "impact", "asset", "view", "auxiliary"]
    description: str = Field(min_length=1)
    clip_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "done"] = "pending"
    evidence_ids: list[str] = Field(default_factory=list)
    resolution: str = ""
    # Scheduling metadata only: never marks a requirement or handoff complete.
    source_waits: list[ProposalSourceWait] = Field(default_factory=list)


class MaterialRequest(Contract):
    kind: Literal["asset", "state", "view", "action", "layout", "target_frame", "auxiliary"]
    description: str = Field(min_length=1)
    asset_id: str | None = None
    state_id: str | None = None
    views: list[str] = Field(default_factory=list)
    defect: str = ""


class Proposal(Contract):
    id: str = Field(default_factory=new_id)
    actor_id: str
    set_id: str
    text: str
    issue_key: str | None = None
    status: Literal["pending", "accepted", "in_progress", "applied", "rejected"] = "pending"
    resolution: str | None = None
    affected_clip_ids: list[str] = Field(default_factory=list)
    requirements: list[ProposalRequirement] = Field(default_factory=list)
    material_requests: list[MaterialRequest] = Field(default_factory=list)
    material_requests_acknowledged_hash: str | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class VisualAnalysisTask(Contract):
    id: str = Field(default_factory=new_id)
    parent_actor_id: str
    actor_id: str
    question: str = Field(min_length=1)
    candidate_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)
    media_ids: list[str] = Field(default_factory=list)
    group_id: str | None = Field(default_factory=lambda: None)
    source_task_id: str | None = Field(default_factory=lambda: None)
    status: Literal["queued", "running", "complete", "failed"] = "queued"
    conclusion: str = ""
    findings: list[VisualFinding] = Field(default_factory=list)
    viewed_media_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_fingerprint: str = ""
    created_at: str = Field(default_factory=now)
    completed_at: str | None = None


DelegateKind = Literal[
    "candidate_review",
    "repair_diagnosis",
    "material_preparation",
    "handoff_verification",
    "revision_impact",
    "state_conflict",
]


class DelegateScope(Contract):
    """Explicit object allowlist. Provenance closure is computed by the backend."""

    asset_ids: list[str] = Field(default_factory=list)
    state_ids: list[str] = Field(default_factory=list)
    set_ids: list[str] = Field(default_factory=list)
    clip_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)
    media_ids: list[str] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    job_ids: list[str] = Field(default_factory=list)
    reference_plan_ids: list[str] = Field(default_factory=list)


class ReferenceReplacementGrant(Contract):
    revision_id: str
    usage_fingerprint: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    impact_task_id: str = Field(min_length=1)
    expected_report_fingerprint: str = Field(min_length=1)


class DelegateRequest(Contract):
    kind: DelegateKind
    question: str = Field(min_length=1)
    scope: DelegateScope = Field(default_factory=DelegateScope)
    evidence_policy: Literal["current", "records-only"] = Field(
        default="current", exclude_if=lambda value: value == "current"
    )
    state_id: str | None = None
    views: list[str] = Field(default_factory=list)
    source_revision_ids: list[str] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    allow_crop: bool = False
    allow_observed_adoption: bool = False
    replacements: list[ReferenceReplacementGrant] = Field(default_factory=list)
    depends_on_task_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    priority: int = 0
    defects: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def records_only_is_static(self) -> Self:
        if self.evidence_policy == "records-only" and (
            self.kind != "state_conflict"
            or self.scope.candidate_ids
            or self.scope.media_ids
            or self.views
            or self.allow_crop
            or self.allow_observed_adoption
        ):
            raise ValueError("仅记录授权只用于无候选、媒体或图像目标的静态状态契约诊断")
        return self


class RepairRecommendation(Contract):
    defect: str = Field(min_length=1)
    evidence_media_ids: list[str] = Field(default_factory=list)
    change: str = Field(min_length=1)
    material_needed: bool
    material_purpose: str = ""
    remaining_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def needed_material_has_purpose(self) -> Self:
        if self.material_needed and not self.material_purpose.strip():
            raise ValueError("需要素材时必须说明素材具体支撑哪个缺陷")
        return self


class HandoffAssessment(Contract):
    set_id: str
    handoff_id: str
    reusable_revision_ids: list[str] = Field(default_factory=list)
    verified_attributes: list[str] = Field(default_factory=list)
    missing_attributes: list[str] = Field(default_factory=list)
    missing_views: list[str] = Field(default_factory=list)
    evidence_media_ids: list[str] = Field(default_factory=list)
    conclusion: str = Field(min_length=1)


class RevisionUseAssessment(Contract):
    candidate_id: str
    disposition: Literal["unaffected", "review_required", "local_adjustment"]
    reason: str = Field(min_length=1)
    evidence_media_ids: list[str] = Field(default_factory=list)


class RevisionImpactAssessment(Contract):
    revision_id: str
    uses: list[RevisionUseAssessment] = Field(default_factory=list)
    pending_configuration_ids: list[str] = Field(default_factory=list)
    pending_configuration_reason: str = ""


class DelegateReport(Contract):
    conclusion: str = Field(min_length=1)
    findings: list[VisualFinding] = Field(default_factory=list)
    repairs: list[RepairRecommendation] = Field(default_factory=list)
    handoffs: list[HandoffAssessment] = Field(default_factory=list)
    revision_impacts: list[RevisionImpactAssessment] = Field(default_factory=list)
    selected_revision_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    blocker: str | None = None
    owner_id: str | None = None
    # Deterministic provenance is populated by the runtime, not narrated by the model.
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class DelegateTask(Contract):
    id: str = Field(default_factory=new_id)
    parent_actor_id: str
    actor_id: str
    request: DelegateRequest
    allowed_scope: DelegateScope
    allowed_actions: list[str]
    input_fingerprint: str
    target_fingerprints: dict[str, str] = Field(default_factory=dict)
    source_fingerprints: dict[str, str] = Field(default_factory=dict)
    usage_fingerprints: dict[str, str] = Field(default_factory=dict)
    blocked_dependency_ids: list[str] = Field(default_factory=list)
    dedupe_key: str
    version: int = Field(default=1, ge=1)
    status: Literal["queued", "running", "complete", "failed", "blocked"] = "queued"
    report: DelegateReport | None = None
    report_fingerprint: str = ""
    viewed_media_ids: list[str] = Field(default_factory=list)
    evidence_fingerprint: str = ""
    output_revision_ids: list[str] = Field(default_factory=list)
    output_media_ids: list[str] = Field(default_factory=list)
    output_job_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    followups: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    blocker: str | None = None
    created_at: str = Field(default_factory=now)
    completed_at: str | None = None
    # Exact existing reservations, frozen at authorization; never infer a later grant.
    repair_supply_targets: dict[str, str] = Field(default_factory=dict)
    delivered_revision_ids: list[str] = Field(default_factory=list)


class ExecutiveInboxItem(Contract):
    id: str = Field(default_factory=new_id)
    parent_actor_id: str
    task_id: str
    task_version: int
    kind: Literal["complete", "blocked", "failed", "followup"]
    priority: int = 0
    summary: str
    report_snapshot: DelegateReport | None = None
    report_fingerprint: str = ""
    viewed_media_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "acknowledged"] = "pending"
    acknowledged_at: str | None = None
    created_at: str = Field(default_factory=now)


class DelegateApproval(Contract):
    task_id: str
    expected_task_version: int = Field(ge=1)
    expected_report_fingerprint: str = Field(min_length=1)
    context_token: str = Field(min_length=1)
    action: Literal[
        "adopt_report",
        "approve_asset_observation",
        "confirm_set_handoff",
        "assess_revision",
        "supply_repair_references",
        "resolve_quality",
        "resolve_proposal",
        "correct_state_scope",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    group_id: str | None = None
    reason: str = Field(min_length=1)


class ExecutiveDocument(Contract):
    id: str = Field(default_factory=new_id)
    actor_id: str
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
    revision: int = Field(default=1, ge=1)
    title: str = Field(min_length=1)
    text: str
    source_refs: list[str] = Field(default_factory=list)
    supersedes_revision: int | None = None
    updated_at: str = Field(default_factory=now)


class ExecutiveDocumentVersion(ExecutiveDocument):
    document_id: str


class Job(Contract):
    id: str = Field(default_factory=new_id)
    actor_id: str
    kind: Literal["image", "video", "analysis", "assembly", "reference"]
    target_id: str
    status: Literal["queued", "submitting", "running", "complete", "failed", "ambiguous"] = "queued"
    provider_job_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=now)
    next_poll_at: float = 0
    poll_attempts: int = 0
    query_error_count: int = 0
    query_outage_since: str | None = None
    last_query_success_at: str | None = None
    query_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    lease_owner: str | None = None
    lease_until: float = 0
    lease_generation: int = Field(default_factory=lambda: 0, ge=0)
    submitted_at: str | None = Field(default_factory=lambda: None)
    completed_at: str | None = Field(default_factory=lambda: None)
    # Explicit user authorization, written only by audited maintenance, never a role tool.
    image_recovery_authorization: dict[str, Any] | None = Field(default_factory=lambda: None)


class ReadinessItem(Contract):
    key: str
    kind: str
    description: str
    owner_id: str
    owner_role: Role
    entity_id: str | None = None
    proposal_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class SetDependencyReport(Contract):
    set_id: str
    policy: Literal["observed-v1"]
    film_order: int
    can_plan: bool
    dependency_revision: int
    items: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[ReadinessItem] = Field(default_factory=list)
    preserved_clip_ids: list[str] = Field(default_factory=list)


class ClipReadiness(Contract):
    clip_id: str
    quality_progress: QualityProgress | None = None
    ready: bool
    selected: bool = False
    items: list[ReadinessItem] = Field(default_factory=list)
    existing_proposal_ids: list[str] = Field(default_factory=list)
    reference_plan_id: str | None = None
    reference_plan_status: str | None = None
    can_plan: bool = False
    can_generate: bool = False
    planning_items: list[ReadinessItem] = Field(default_factory=list)


class ProductionBlocker(Contract):
    key: str
    kind: str
    description: str
    owner_id: str | None = None
    owner_role: str
    entity_id: str | None = None
    set_id: str | None = None
    clip_id: str | None = None
    unknown_wait: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ProductionMaterial(Contract):
    state_id: str
    asset_id: str
    label: str
    status: Literal[
        "ready", "preparing", "review", "waiting_source", "needed", "not_required", "blocked"
    ]
    required_by: list[str] = Field(default_factory=list)
    missing_views: list[str] = Field(default_factory=list)
    source_set_id: str | None = None
    source_event_id: str | None = None
    active_job_ids: list[str] = Field(default_factory=list)
    owner_id: str | None = None


class ProductionActorStatus(Contract):
    actor_id: str
    set_id: str | None = None
    clip_id: str | None = None
    label: str
    can_plan: bool = False
    can_generate: bool = False
    allowed_operations: list[str] = Field(default_factory=list)
    blocker_keys: list[str] = Field(default_factory=list)


class ProductionETA(Contract):
    status: Literal["estimated", "conditional", "complete", "unavailable"]
    minimum_seconds: float | None = None
    maximum_seconds: float | None = None
    confidence: Literal["low", "medium", "none"] = "low"
    earliest_at: str | None = None
    latest_at: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)
    sample_counts: dict[str, int] = Field(default_factory=dict)
    unplanned_sets: int = 0
    estimated_unplanned_clips: int = 0
    rework_per_clip: float = 0


class PhaseTiming(Contract):
    phase: str
    count: int
    total_seconds: float
    sample_count: int
    started_at: float
    ended_at: float
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    successful_sample_count: int = 0
    successful_p50_seconds: float | None = None
    sample_active_seconds: float
    database_seconds: float = 0
    inclusive_of: list[str] = Field(default_factory=list)


class ProductionStatus(Contract):
    project_id: str
    revision: int
    as_of: str = Field(default_factory=now)
    status: str
    selected_clips: int
    planned_clips: int
    planned_sets: int
    total_sets: int
    eta: ProductionETA
    playback: PlaybackDuration | None = Field(default_factory=lambda: None)
    blockers: list[ProductionBlocker] = Field(default_factory=list)
    actors: list[ProductionActorStatus] = Field(default_factory=list)
    materials: list[ProductionMaterial] = Field(default_factory=list)
    material_counts: dict[str, int] = Field(default_factory=dict)
    timings: list[PhaseTiming] = Field(default_factory=list)
    elapsed_seconds: float = 0
    notes: list[str] = Field(default_factory=list)
    lifecycle: dict[str, Any] | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )


class ReferenceOption(Contract):
    id: str
    modality: Literal["image", "video", "audio"]
    label: str
    purpose: str
    media_id: str | None = None
    asset_id: str | None = None
    state_id: str | None = None
    revision_id: str | None = None
    preparation_job_id: str | None = None
    source_candidate_id: str | None = None
    voice_reference_id: str | None = None
    state_role: Literal["start", "target", "guidance"] | None = None
    roles: list[str] = Field(default_factory=list)
    preparation: Literal["ready", "inspect", "crop_or_use_board", "excerpt"]
    steps: list[str] = Field(default_factory=list)
    duration: float | None = None


class ReferenceOptions(Contract):
    clip_id: str
    groups: dict[str, list[ReferenceOption]] = Field(default_factory=dict)
    limits: dict[str, int] = Field(
        default_factory=lambda: {
            "images": 9,
            "videos": 3,
            "audio": 3,
            "mixed": 12,
            "video_seconds": 15,
            "audio_seconds": 15,
        }
    )
    boundary_image_slots: int = 0
    next_cursor: str | None = None


class ComparedCandidate(Contract):
    id: str
    attempt: int
    status: str
    media_id: str | None = None
    selected: bool = False
    selection: SelectionRecord | None = None
    review: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    required_evidence_count: int = 0


class CandidateComparison(Contract):
    clip_id: str
    candidates: list[ComparedCandidate] = Field(default_factory=list)
    default_pair: list[str] = Field(default_factory=list)
    next_cursor: str | None = None


class TranscriptItem(Contract):
    id: str
    actor_id: str
    kind: Literal["message", "tool", "memory_change", "asset_change", "status", "error", "user"]
    text: str = ""
    status: str = "complete"
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    sequence: int = 0
    version: int = 0
    body_ref: str | None = None
    text_length: int | None = None
    activity: dict[str, Any] = Field(default_factory=dict)


class StudioEvent(Contract):
    cursor: int
    project_id: str
    actor_id: str | None = None
    kind: str
    item_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)


class EventBatch(Contract):
    events: list[StudioEvent]
    cursor: int
    has_more: bool = False


class StudioProject(Contract):
    id: str = Field(default_factory=new_id)
    revision: int = 0
    title: str
    brief: str
    status: ProjectStatus = "screenwriting"
    deleted_at: str | None = Field(default_factory=lambda: None)
    config: StudioConfig = Field(default_factory=StudioConfig)
    workflow_version: WorkflowVersion = "set-v1"
    harness_version: HarnessVersion = "adaptive-v2"
    handoff_policy: Literal["observed-v1"] = "observed-v1"
    playback_policy: Literal["set-budget-v1"] = "set-budget-v1"
    director_mode: DirectorMode = "current"
    set_director_policy: Literal["efficient-v1"] = "efficient-v1"
    planning_context_policy: Literal["clip-round-v1"] = "clip-round-v1"
    production_efficiency_policy: Literal["task-v1"] = "task-v1"
    api_config_snapshot: dict[str, Any] = Field(default_factory=dict)
    screenplay: Screenplay = Field(default_factory=Screenplay)
    catalog_frozen_at: str | None = None
    global_memory: Memory = Field(default_factory=Memory)
    memory_schema_version: int = 2
    story_events: dict[str, StoryEvent] = Field(default_factory=dict)
    reference_plans: dict[str, ReferencePlan] = Field(default_factory=dict)
    context_reads: dict[str, ContextRead] = Field(default_factory=dict)
    actors: dict[str, Actor] = Field(default_factory=dict)
    sets: dict[str, ProductionSet] = Field(default_factory=dict)
    clips: dict[str, Clip] = Field(default_factory=dict)
    assets: dict[str, LogicalAsset] = Field(default_factory=dict)
    states: dict[str, AssetState] = Field(default_factory=dict)
    references: dict[str, ReferenceRevision] = Field(default_factory=dict)
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    jobs: dict[str, Job] = Field(default_factory=dict)
    media: dict[str, Media] = Field(default_factory=dict)
    image_quarantines: dict[str, ImageQuarantine] = Field(default_factory=dict)
    external_reviews: dict[str, ExternalMediaReview] = Field(default_factory=dict)
    visual_tasks: dict[str, VisualAnalysisTask] = Field(default_factory=dict)
    delegate_tasks: dict[str, DelegateTask] = Field(default_factory=dict)
    executive_inbox: dict[str, ExecutiveInboxItem] = Field(default_factory=dict)
    executive_documents: dict[str, ExecutiveDocument] = Field(default_factory=dict)
    executive_document_versions: dict[str, ExecutiveDocumentVersion] = Field(default_factory=dict)
    transcript: dict[str, TranscriptItem] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    final_media_id: str | None = None
    export_manifest: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class ProjectSummary(Contract):
    id: str
    revision: int
    title: str
    brief: str
    status: ProjectStatus
    deleted_at: str | None = Field(default_factory=lambda: None)
    workflow_version: WorkflowVersion = "set-v1"
    updated_at: str
    set_count: int = 0
    clip_count: int = 0
    accepted_clip_count: int = 0
    paused: bool = False
    preview_media: Media | None = Field(default_factory=lambda: None)


class ProjectList(Contract):
    projects: list[ProjectSummary]
    next_cursor: str | None = None
    has_more: bool = False


class StateReferenceUse(Contract):
    """Read-only tree classification, separate from immutable production records."""

    kind: Literal["reference", "pending", "process", "unverified"]
    basis: list[str] = Field(default_factory=list)
    set_ids: list[str] = Field(default_factory=list)
    clip_ids: list[str] = Field(default_factory=list)
    video_media_ids: list[str] = Field(default_factory=list)


class AssetStateTree(Contract):
    asset_id: str
    states: dict[str, StateReferenceUse] = Field(default_factory=dict)


class StudioSnapshot(Contract):
    """A scoped projection, never a complete history-bearing aggregate."""

    project: StudioProject
    cursor: int
    scope: str = "overview"
    entity_versions: dict[str, int] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    state_tree: AssetStateTree | None = Field(default_factory=lambda: None)


class CommandResult(Contract):
    project_id: str
    revision: int
    cursor: int
    events: list[StudioEvent] = Field(default_factory=list)


class TranscriptPage(Contract):
    items: list[TranscriptItem]
    next_cursor: int | None = None
    has_more: bool = False
    cursor: int


class EntityLabel(Contract):
    label: str = Field(min_length=1, max_length=240)
    description: str = Field(max_length=500)
    kind: str


class EntityLabelBatch(Contract):
    labels: dict[str, EntityLabel]
    missing_ids: list[str]


class EntityPage(Contract):
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    has_more: bool = False
    cursor: int
    related: dict[str, dict[str, Any]] = Field(default_factory=dict)
    entity_versions: dict[str, int] = Field(default_factory=dict)
    ancestor_ids: list[str] = Field(default_factory=list)


class BodyChunk(Contract):
    text: str
    offset: int
    next_offset: int | None = None
    total: int
    content_type: str = "application/json"
    body_ref: str | None = None


class CreateProject(Contract):
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1)
    idempotency_key: str | None = Field(default_factory=lambda: None, min_length=1, max_length=128)
    config: StudioConfig = Field(default_factory=StudioConfig)
    director_mode: DirectorMode = Field(default_factory=lambda: "delegated-v1")


class UserAction(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    text: str = ""
    director_mode: DirectorMode | None = Field(default_factory=lambda: None)


class ExternalReviewRequest(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reviewer_id: ExternalReviewIdentifier
    authorization_id: ExternalReviewIdentifier
    reason: ExternalReviewText
    scope: ExternalReviewScope
    items: list[ExternalReviewItem] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_media(self) -> Self:
        media_ids = [item.media_id for item in self.items]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("同一外部审核请求不能重复包含素材")
        return self


class ExternalCandidateReviewRequest(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reviewer_id: ExternalReviewIdentifier
    authorization_id: ExternalReviewIdentifier
    reason: ExternalReviewText
    external_review_ids: list[str] = Field(min_length=1, max_length=128)
    decision: Literal["accept", "reject"]
    findings: list[VisualFinding] = Field(default_factory=list)
    evidence_media_ids: list[str] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    batch_assessments: list[ReviewFindingAssessment] = Field(default_factory=list)
    coverage_gaps: list[ReviewGap] = Field(default_factory=list)
    selection_assessment: CandidateSelectionAssessment | None = None
    event_observations: list[EventObservation] = Field(default_factory=list)
    review_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("external_review_ids", "evidence_media_ids")
    @classmethod
    def unique_review_lists(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("外部审核引用不能重复")
        return value


class ExternalOperatorTakeoverRequest(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reviewer_id: ExternalReviewIdentifier
    authorization_id: ExternalReviewIdentifier
    reason: ExternalReviewText
    action: Literal["resume", "reset_context"]


class ClipPreparationUpdate(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_preparation_id: str | None = None
    enabled: bool = True
    action_notes: str = ""
    prompt_draft: str = ""
    prepare_next_clip: bool | None = None


class ToolCommand(Contract):
    expected_revision: int | None = Field(default=None, ge=0)
    read_token: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    action: Literal[
        "save_screenplay",
        "write_memory",
        "correct_memory_scope",
        "plan_story_events",
        "update_clip_context",
        "save_reference_plan",
        "finalize_reference_plan",
        "patch_reference_plan",
        "save_clip_preparation",
        "finish_review_batch",
        "request_review_batch",
        "delegate_visual_analysis",
        "complete_visual_analysis",
        "delegate_tasks",
        "complete_delegate_task",
        "request_delegate_followup",
        "approve_delegate_results",
        "acknowledge_executive_inbox",
        "save_executive_document",
        "finish_initial_planning",
        "supply_repair_references",
        "add_asset_views",
        "create_asset",
        "create_state",
        "correct_state_scope",
        "record_state_evolution",
        "generate_reference",
        "prepare_reference",
        "adopt_prepared_reference",
        "approve_reference_region",
        "approve_reference_coverage",
        "prepare_media",
        "select_reference",
        "review_reference",
        "resolve_quality",
        "plan_voice",
        "establish_voice",
        "select_voice",
        "approve_asset_observation",
        "prepare_sets",
        "set_duration_budget",
        "publish_set_context",
        "configure_set_dependencies",
        "configure_state_preparation",
        "confirm_set_handoff",
        "adopt_observed_reference",
        "plan_clips",
        "freeze_production",
        "propose_global",
        "resolve_proposal",
        "generate_clip",
        "analyze_candidate",
        "review_candidate",
        "confirm_candidate_state",
        "repair_clip",
        "assess_revision",
        "reconcile_job",
        "set_status",
        "finish_set",
        "assemble",
        "finish_project",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
