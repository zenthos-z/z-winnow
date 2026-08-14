"""RL training data Pydantic models — four-tuple (state, action, reward, metadata).

Defines the nested data structures that map to the JSONL format defined in
schemas/rl_record_v1.json. Uses Pydantic v2 with ConfigDict(extra='forbid')
strict mode to ensure extractor output is schema-compliant.

Model hierarchy:
    RLRecord (top-level JSONL row)
    ├── RLState (context snapshot)
    ├── RLAction (agent decision/output)
    ├── RLReward (feedback signal)
    │   ├── RLRewardDimension (four quality axes)
    │   └── RLRewardSignal (individual signal entries)
    └── RLMetadata (provenance + statistics)
        └── RLProvenance (three-layer traceability)

Reference:
    schemas/rl_record_v1.json — JSON Schema definition
    docs/rl-schema-v1.md — Schema usage documentation
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# RLProvenance — three-layer traceability
# ============================================================


class RLProvenance(BaseModel):
    """Provenance info tracing RL data back to SQLite three-layer source.

    Attributes:
        raw_message_count: Number of raw messages (Layer 1).
        parsed_context_ids: Context IDs from Layer 2 parsed_contexts table.
        topic_summary_ids: Topic summary IDs from Layer 3 topic_summaries table.
    """

    raw_message_count: int = Field(
        default=0,
        ge=0,
        description="Number of original messages (Layer 1 raw_messages).",
    )
    parsed_context_ids: list[str] = Field(
        default_factory=list,
        description="Context IDs referencing Layer 2 parsed_contexts.",
    )
    topic_summary_ids: list[str] = Field(
        default_factory=list,
        description="Topic summary IDs referencing Layer 3 topic_summaries.",
    )

    model_config = ConfigDict(extra="forbid")


# ============================================================
# RLState — context snapshot
# ============================================================


class RLState(BaseModel):
    """Context snapshot — what the agent saw when making the decision.

    Corresponds to the `state` block in JSONL format. Sources data from:
      - server_ids → Layer 1 raw_messages
      - parsed_context_ids → Layer 2 parsed_contexts
      - history_topics → Layer 3 topic_summaries

    Attributes:
        messages_summary: Concise summary of the day's messages (1-3 sentences).
        context_token_count: Estimated token count of the context.
        history_topics: Historical topic IDs for cross-day tracking.
        server_ids: Source message serverIDs (provenance root).
        parsed_context_ids: Parsed context block IDs.
        active_members: Active chat member identifiers.
        message_count: Total message count for the day.
    """

    messages_summary: str = Field(
        default="",
        description="Brief summary of the day's messages (1-3 sentences).",
    )
    context_token_count: int = Field(
        default=0,
        ge=0,
        description="Estimated token count of context.",
    )
    history_topics: list[str] = Field(
        default_factory=list,
        description="Historical topic IDs from previous days.",
    )
    server_ids: list[str] = Field(
        default_factory=list,
        description="Source message serverIDs — root of provenance chain.",
    )
    parsed_context_ids: list[str] = Field(
        default_factory=list,
        description="Parsed context block IDs (Layer 2).",
    )
    active_members: list[str] = Field(
        default_factory=list,
        description="Active member identifiers for the day.",
    )
    message_count: int = Field(
        default=0,
        ge=0,
        description="Total message count for the date.",
    )

    model_config = ConfigDict(extra="forbid")


# ============================================================
# RLAction — agent decision/output
# ============================================================


class RLAction(BaseModel):
    """Agent decision or output — the behavior being evaluated.

    Attributes:
        agent: Subagent identifier (e.g. daily_report_generator).
        decision: Human-readable decision description.
        model_used: Model identifier used for generation.
        temperature: Generation temperature parameter.
        latency_ms: End-to-end latency in milliseconds.
        report_sections: Daily report section names (preference_alignment).
        routing_decisions: Per-agent routing decisions (routing_decision).
        status_classifications: Topic status classifications (topic_tracking).
        prompt_version: Prompt version identifier for reproducibility.
    """

    agent: str = Field(
        default="",
        description="Subagent identifier (e.g. daily_report_generator, orchestrator).",
    )
    decision: str = Field(
        default="",
        description="Human-readable decision description.",
    )
    model_used: str = Field(
        default="mock",
        description="Model identifier used for generation.",
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Generation temperature parameter.",
    )
    latency_ms: int = Field(
        default=0,
        ge=0,
        description="End-to-end latency in milliseconds.",
    )
    report_sections: list[str] = Field(
        default_factory=list,
        description="Daily report section names (preference_alignment scenario).",
    )
    routing_decisions: list[dict] = Field(
        default_factory=list,
        description="Per-agent routing enable/disable decisions (routing_decision scenario).",
    )
    status_classifications: list[dict] = Field(
        default_factory=list,
        description="Topic status classifications (topic_tracking scenario).",
    )
    prompt_version: str = Field(
        default="",
        description="Prompt version identifier for reproducibility.",
    )

    model_config = ConfigDict(extra="forbid")


# ============================================================
# RLReward — feedback signal
# ============================================================


class RLRewardDimension(BaseModel):
    """Four quality dimensions for multi-axis reward evaluation.

    Dimensions are optional per-scenario:
        preference_alignment → completeness + conciseness required
        routing_decision → accuracy required
        topic_tracking → accuracy required
    """

    completeness: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Completeness score [0, 1] — are all required sections present?",
    )
    accuracy: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Accuracy score [0, 1] — is the content correct?",
    )
    conciseness: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Conciseness score [0, 1] — is it concise and non-redundant?",
    )
    actionability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Actionability score [0, 1] — are conclusions executable?",
    )

    model_config = ConfigDict(extra="forbid")


class RLRewardSignal(BaseModel):
    """A single reward signal entry within the signals[] array.

    Attributes:
        signal_type: Type — rule_based / auto_comparison / human_feedback.
        source: Origin — system_check / llm_judge / old_system / human_annotator / user_feedback.
        value: Normalized reward value [0.0, 1.0].
        dimension: Quality dimension label (e.g. "completeness").
        evidence: Human-readable justification for the score.
        timestamp: ISO 8601 timestamp of signal generation.
    """

    signal_type: str = Field(
        default="rule_based",
        description="Signal type: rule_based | auto_comparison | human_feedback.",
    )
    source: str = Field(
        default="system_check",
        description="Signal source: system_check | llm_judge | old_system | human_annotator | user_feedback.",
    )
    value: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Normalized reward value [0.0, 1.0].",
    )
    dimension: str = Field(
        default="",
        description="Quality dimension label (completeness, accuracy, conciseness, actionability).",
    )
    evidence: str = Field(
        default="",
        description="Human-readable justification for the reward value.",
    )
    timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of signal generation.",
    )

    model_config = ConfigDict(extra="forbid")


class RLReward(BaseModel):
    """Aggregated reward for an RL record.

    Attributes:
        source: Dominant reward source (highest-priority signal source).
        score: Aggregated reward score [0.0, 1.0] — mean of all signal values.
        dimensions: Per-dimension quality scores.
        signals: Detailed reward signal entries.
    """

    source: str = Field(
        default="system_check",
        description="Dominant reward source: system_check | llm_judge | old_system | human_annotator | user_feedback.",
    )
    score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Aggregated reward score — mean of all signal values.",
    )
    dimensions: RLRewardDimension = Field(
        default_factory=RLRewardDimension,
        description="Per-dimension quality scores.",
    )
    signals: list[RLRewardSignal] = Field(
        default_factory=list,
        description="Detailed reward signal entries.",
    )

    model_config = ConfigDict(extra="forbid")


# ============================================================
# RLMetadata — provenance and statistics
# ============================================================


class RLMetadata(BaseModel):
    """Metadata — provenance trace and auxiliary statistics.

    Attributes:
        timestamp: ISO 8601 record creation timestamp.
        version: Schema version, fixed to "1.0" for v1.
        provenance: Three-layer data provenance references.
        trace_id: Optional LangSmith trace ID.
        report_char_count: Report character count (preference_alignment).
        total_agents: Total available agents (routing_decision).
        enabled_agent_count: Enabled agents count (routing_decision).
        history_topic_count: Historical topic count (topic_tracking).
        affected_topic_count: Affected topic count (topic_tracking).
    """

    timestamp: str = Field(
        default="",
        description="ISO 8601 record creation timestamp.",
    )
    version: str = Field(
        default="1.0",
        description="Schema version, fixed to '1.0' for v1.",
    )
    provenance: RLProvenance = Field(
        default_factory=RLProvenance,
        description="Three-layer provenance — raw_messages → parsed_contexts → topic_summaries.",
    )
    trace_id: str = Field(
        default="",
        description="LangSmith trace ID for observability.",
    )
    report_char_count: int = Field(
        default=0,
        ge=0,
        description="Report character count (preference_alignment).",
    )
    total_agents: int = Field(
        default=0,
        ge=0,
        description="Total available subagents (routing_decision).",
    )
    enabled_agent_count: int = Field(
        default=0,
        ge=0,
        description="Enabled subagents count (routing_decision).",
    )
    history_topic_count: int = Field(
        default=0,
        ge=0,
        description="Historical topic count (topic_tracking).",
    )
    affected_topic_count: int = Field(
        default=0,
        ge=0,
        description="Affected topic count (topic_tracking).",
    )

    model_config = ConfigDict(extra="forbid")


# ============================================================
# RLRecord — top-level JSONL row
# ============================================================


class RLRecord(BaseModel):
    """A single RL training record — one JSONL row.

    Bundles the complete four-tuple (state, action, reward, metadata)
    along with top-level identification fields.

    Attributes:
        record_id: Globally unique ID, format "rl-{YYYYMMDD}-{seq}".
        date: Data date in YYYY-MM-DD format.
        scenario: Training scenario — preference_alignment | routing_decision | topic_tracking.
        state: Context snapshot the agent saw.
        action: Agent decision or output.
        reward: Feedback signal (may be None before scoring).
        metadata: Provenance and auxiliary statistics.
    """

    record_id: str = Field(
        description="Globally unique RL record ID, format 'rl-{YYYYMMDD}-{seq}'.",
        pattern=r"^rl-\d{8}-\d{3,}$",
    )
    date: str = Field(
        description="Data date in YYYY-MM-DD format.",
    )
    scenario: str = Field(
        description="Training scenario: preference_alignment | routing_decision | topic_tracking.",
    )
    state: RLState = Field(
        default_factory=RLState,
        description="Context snapshot — what the agent observed.",
    )
    action: RLAction = Field(
        default_factory=RLAction,
        description="Agent decision/output — the behavior being evaluated.",
    )
    reward: RLReward | None = Field(
        default=None,
        description="Feedback signal (None before reward computation).",
    )
    metadata: RLMetadata = Field(
        default_factory=RLMetadata,
        description="Provenance trace and auxiliary statistics.",
    )

    model_config = ConfigDict(extra="forbid")
