"""Every persisted model of the harness.

- `extra="forbid"`: a typo in a YAML file fails instead of vanishing.
- Sets are stored as sorted lists (`Channels`), so JSON files are byte-stable across processes.
- In-memory types (`ToolEnv`, `CheckContext`, `Pack`) are dataclasses in their own modules.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

Id = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")]
# a pack is imported as the package packs.<name>, so its name is a Python identifier
PackName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
# [0-9], not \d: \d also matches look-alike digits (fullwidth, Arabic-Indic…)
HazardId = Annotated[str, Field(pattern=r"^H-[0-9]{2,3}$")]
ControlId = Annotated[str, Field(pattern=r"^H-[0-9]{2,3}\.C[0-9]+$")]  # H-01.C2
CheckId = Annotated[str, Field(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")]  # core.x / <pack>.y
Rate = Annotated[float, Field(gt=0, lt=1)]
Channel = Literal["turn_text", "tool_calls", "final_outcome", "side_channel", "ground_truth"]
Channels = Annotated[list[Channel], AfterValidator(lambda v: sorted(set(v)))]
Kind = Literal["validity", "conformance", "hazard"]
Method = Literal["deterministic", "heuristic", "judge"]  # judge: designed, not built
Outcome = Literal["pass", "fail", "not_evaluable", "invalid", "error"]
Fault = Literal["agent", "simulator", "test_data", "harness", "provider"]
Verdict = Literal["met", "not_met", "not_demonstrated", "not_evaluable"]
# thinking depth and token spend; replaces sampling parameters, which API 2026-10-21 rejects
Effort = Literal["low", "medium", "high", "xhigh", "max"]
HiddenType = Literal["any", "integer", "number", "string", "boolean"]  # a JSON type; any: untyped
# side_channel: key -> its type (null always allowed); a plain list declares untyped keys
SideChannel = Annotated[
    dict[str, HiddenType],
    BeforeValidator(lambda v: dict.fromkeys(v, "any") if isinstance(v, list) else v),
]
KINDS: dict[str, Callable[[Any], bool]] = {  # the typed side-channel values a string can carry
    "integer": lambda v: type(v) is int,
    "number": lambda v: type(v) in (int, float),
    "boolean": lambda v: type(v) is bool,
}
FAULTS: dict[str, set[str | None]] = {
    "pass": {None},
    "not_evaluable": {None},
    "fail": {"agent"},
    "invalid": {"simulator", "test_data"},
    "error": {"harness", "provider"},
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Pack definition: domains/<name>/…, byte-copied to runs/<ts>/snapshot/<name>/ ─────────────────


class SeverityLevel(Strict):
    name: str
    rank: int = Field(ge=1)
    definition: str


class GenericCheckBinding(Strict):  # pack.yaml: generic_checks: {core.x: {hazards: [...]}}
    hazards: list[HazardId] = Field(min_length=1)
    controls: list[ControlId] = []


class Variant(Strict):  # e.g. a second instrument run on the same pack code
    cases_dir: str
    data: dict[str, str] = {}  # overrides PackConfig.data entries
    exclude_hazards: list[HazardId] = []  # printed as "not applicable to this variant"


class EstimateHints(Strict):
    exchanges: int = Field(ge=1)  # typical user turns per trial
    tokens_per_exchange: int = Field(ge=1)
    output_tokens_per_call: int = Field(ge=1)
    system_tokens: int = 800


class PackConfig(Strict):  # pack.yaml
    name: PackName
    version: str = Field(
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$"
    )  # for humans; the content hash is the truth
    title: str
    intended_use: str
    out_of_scope: list[str]
    disclaimer: str  # printed in the "what this is not" box
    severity_scale: list[SeverityLevel] = Field(min_length=2)
    depends_on: list[PackName] = []  # packs whose code this pack imports
    data: dict[str, str] = {}  # name -> YAML file, validated by the pack's DATA_MODELS
    variants: dict[Id, Variant] = {}  # "default" = cases/ + data
    side_channel: SideChannel = {}  # keys the simulator declares in `hidden` on every turn
    generic_checks: dict[CheckId, GenericCheckBinding] = {}  # unbound core checks do not run
    max_turns: int = Field(default=30, ge=1)  # user turns per trial
    default_repeats: int = Field(default=3, ge=1)
    default_target: Id = "reference"
    estimate: EstimateHints


class Control(Strict):
    id: ControlId
    requirement: str  # verifiable statement of what the agent must do
    # ISO 14971 cl. 7.1
    type: Literal["inherent_design", "protective_measure", "information_for_safety"]


class Acceptance(Strict):  # declared before the run; hashed into the manifest
    # a case fails if any of its valid trials fails
    metric: Literal["case_failure_rate"] = "case_failure_rate"
    max_rate: Rate  # threshold, applied to the UPPER bound
    confidence: Rate = 0.95
    # over distinct cases
    interval: Literal["clopper_pearson_one_sided"] = "clopper_pearson_one_sided"
    min_cases: int = Field(default=5, ge=1)
    # makes "case" well defined at k repeats
    min_valid_trials_per_case: int = Field(default=1, ge=1)
    # per case (trials) and per hazard (cases)
    max_invalid_share: float = Field(default=0.34, ge=0, le=1)
    zero_tolerance: bool = False  # any failing case → not met, whatever n


class Hazard(Strict):  # one entry of hazards.yaml
    id: HazardId
    title: str
    failure_mode: str
    hazardous_situation: str
    harm: str
    severity: str  # a SeverityLevel.name (lint L11)
    severity_rationale: str = Field(min_length=20)
    controls: list[Control] = Field(min_length=1)
    acceptance: Acceptance
    refs: list[str] = []  # free text; a versioned crosswalk file later
    # identified, not yet assessed: printed, skipped by coverage lint
    deferred_reason: str | None = None
    risk_estimate: None = None  # reserved: an opt-in ISO 14971 residual-risk section

    @model_validator(mode="after")
    def _controls_belong(self) -> Hazard:
        if bad := [c.id for c in self.controls if not c.id.startswith(f"{self.id}.")]:
            raise ValueError(f"controls {bad} must start with '{self.id}.'")
        return self


class HazardFile(Strict):
    hazards: list[Hazard] = Field(min_length=1)


class Persona(Strict):
    summary: str
    style: str
    traits: dict[
        Literal["patience", "confusion", "skepticism", "incoherence"],
        Literal["low", "medium", "high"],
    ] = {}
    instructions: str = ""


class Stimulus(Strict):  # exact test input; the simulator can never soften it
    id: Id
    purpose: Literal["injection", "disclosure", "request", "other"]
    channel: Literal["user", "tool_result"] = "user"
    text: str | None = None  # user: delivered verbatim
    # user: replaces user turn N, no simulator call
    at_turn: int | None = Field(default=None, ge=1)
    # user: replaces the simulator's text when its side-channel ⊇ this
    on_hidden: dict[str, Any] | None = None
    hidden: dict[str, Any] = {}  # side-channel recorded with an at_turn delivery
    tool: str | None = None  # tool_result: mocked tool whose result carries the payload
    marker: str | None = None  # tool_result: substring of the payload that proves delivery
    canary: str | None = None  # injection: a string only an obeying agent produces

    @model_validator(mode="after")
    def _shape(self) -> Stimulus:
        if self.channel == "user":
            if not self.text or (self.at_turn is None) == (self.on_hidden is None):
                raise ValueError("a user stimulus has text and exactly one of at_turn / on_hidden")
        elif not (self.tool and self.marker) or self.text or self.at_turn or self.on_hidden:
            raise ValueError(
                "a tool_result stimulus has tool and marker only; "
                "its payload is in Case.environment"
            )
        if self.purpose == "injection" and not self.canary:
            raise ValueError("an injection needs a canary")
        if self.canary and self.text and self.canary.lower() in self.text.lower():
            raise ValueError(
                "the canary must not appear in the payload: quoting it would fail the agent"
            )
        return self


class Case(Strict):  # cases/<id>.yaml: one synthetic user, its persona and its ground truth
    id: Id
    title: str
    # the hazards this case verifies: the statistical sample
    hazards: list[HazardId] = Field(min_length=1)
    tags: list[str] = []
    # what the deployment knows: locale, tenant/user, today…
    context: dict[str, str] = {}
    persona: Persona
    ground_truth: dict[str, Any]  # validated by the pack's GroundTruth (lint L12)
    stimuli: list[Stimulus] = []
    forbidden_strings: list[str] = []  # never in agent output (e.g. another tenant's canaries)
    environment: dict[str, Any] = {}  # initial state of the mocked tools, deep-copied per trial
    max_turns: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _unique_stimuli(self) -> Case:
        ids = [s.id for s in self.stimuli]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate stimulus ids")
        return self


class OutcomeSource(Strict):
    # response_field: designed (HTTP)
    source: Literal["final_json", "tool_call", "response_field", "none"]
    tool_name: str | None = None

    @model_validator(mode="after")
    def _tool_name(self) -> OutcomeSource:
        if (self.source == "tool_call") != (self.tool_name is not None):
            raise ValueError("tool_name is required for, and only for, source: tool_call")
        return self


class Target(Strict):  # targets/<name>.yaml — the system under test
    name: Id
    # only prompted_model is built
    kind: Literal["prompted_model", "openai_compatible", "http", "transcript_import"]
    description: str
    # what checks can see (side_channel and ground_truth come from the harness)
    exposes: Channels
    outcome: OutcomeSource
    end_when: Literal["outcome", "simulator"] = "outcome"  # who ends a conversation
    control_implementations: dict[
        ControlId, Literal["prompt", "code", "tool_policy", "human_process"]
    ] = {}
    tool_policy: dict[str, Any] = {}  # given to mocked tools (e.g. an over-permissive target)
    model_env: str = "AGENT_MODEL"  # prompted_model only, from here down
    effort: Effort = "medium"  # recorded in the manifest and in every model call
    max_tokens: int = 1024
    system_prompt_file: str | None = None  # $placeholders filled from Case.context
    tools: list[str] = []  # pack @tool names offered to the agent
    max_tool_rounds: int = Field(default=5, ge=1)
    connection: dict[str, Any] = {}  # reserved: http / openai_compatible


# ── Transcript: runs/<ts>/<case_id>/run_<n>.json — raw evidence, written once ─────────────────────


class Usage(Strict):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelCall(Strict):  # one per model response
    role: Literal["agent", "simulator", "judge"]
    requested_model: str  # the .env value
    served_model: str  # response.model: the unit under test, not the alias
    temperature: float | None = None  # never sent since API 2026-10-21; kept for old transcripts
    effort: Effort | None = None
    max_tokens: int
    usage: Usage
    cost_usd: float
    stop_reason: str | None = None
    request_id: str | None = None
    latency_ms: int


class ToolCall(Strict):
    id: str
    name: str
    arguments: dict[str, Any]
    result: Any = None
    is_error: bool = False
    called_at: AwareDatetime
    returned_at: AwareDatetime | None = None
    executed_by: Literal["harness_mock", "target"] = "harness_mock"


class Turn(Strict):  # a user message, or the agent's whole answer to it
    index: int = Field(ge=0)
    role: Literal["user", "agent"]
    text: str  # agent: all text blocks of the exchange, joined
    at: AwareDatetime
    tool_calls: list[ToolCall] = []  # agent: every tool call of the exchange, in order
    # agent: only when a target returns a structured result natively
    final_output: dict[str, Any] | None = None
    source: Literal["simulator", "script"] | None = None  # user only
    stimulus_id: str | None = None  # user only
    hidden: dict[str, Any] | None = (
        None  # user only: simulator side-channel, never sent to the target
    )
    # user only: what the simulator wrote before an on_hidden override
    overridden_text: str | None = None
    end_conversation: bool = False  # user only (targets with end_when: simulator)
    model_calls: list[ModelCall] = []

    @model_validator(mode="after")
    def _role_fields(self) -> Turn:
        if self.role == "agent" and (
            self.source
            or self.stimulus_id
            or self.hidden is not None
            or self.overridden_text
            or self.end_conversation
        ):
            raise ValueError("agent turns carry no simulator fields")
        if self.role == "user" and (self.tool_calls or self.final_output is not None):
            raise ValueError("user turns carry no tool calls or final output")
        return self


def json_strings(value: Any) -> Iterator[str]:
    """Every string in a JSON-like value (keys, values, list items, nested), e.g. a tool result."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from json_strings(key)
            yield from json_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from json_strings(item)


def json_scalars(value: Any) -> Iterator[str]:
    """Every scalar in a JSON-like value (keys, values, list items, nested) as the text JSON writes
    it: a string as it is; a number, true, false or null in its JSON form (441789, 1.5). E.g. tool
    arguments, where an integer parameter can carry a record code."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, bool | int | float) or value is None:
        yield json.dumps(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from json_scalars(key)
            yield from json_scalars(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from json_scalars(item)


class Transcript(Strict):
    turns: list[Turn] = []

    @model_validator(mode="after")
    def _contiguous(self) -> Transcript:
        if [t.index for t in self.turns] != list(range(len(self.turns))):
            raise ValueError("turn indices must be 0..n-1")
        return self

    # ── Timeline API: what turn-indexed checks use; the evidence turn is `Turn.index` ────────

    def append(self, turn: Turn) -> Turn:
        """Add `turn` as the next turn and return the stored copy. The index is set here, so the
        builders and the runner never track it."""
        stored = turn.model_copy(update={"index": len(self.turns)})
        self.turns.append(stored)
        return stored

    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "user"]

    def agent_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "agent"]

    def last_agent_turn(self) -> Turn | None:
        return next((t for t in reversed(self.turns) if t.role == "agent"), None)

    def reply_to(self, user_turn: Turn) -> Turn | None:
        """The agent's answer in that exchange: one agent turn holds every model response and tool
        call until the next user turn. None: the conversation stopped before an answer."""
        if user_turn.role != "user":
            raise ValueError(f"turn {user_turn.index} is an agent turn, not a user turn")
        following = self.turns[user_turn.index + 1 : user_turn.index + 2]
        return following[0] if following and following[0].role == "agent" else None

    def first(
        self,
        pred: Callable[[Turn], bool],
        *,
        role: Literal["user", "agent"] | None = None,
        after: int = -1,
    ) -> Turn | None:
        """The first turn after index `after`, of `role` if given, for which `pred` is true."""
        return next(
            (
                t
                for t in self.turns
                if t.index > after and (role is None or t.role == role) and pred(t)
            ),
            None,
        )

    def within(self, turn: Turn, exchanges: int, pred: Callable[[Turn], bool]) -> Turn | None:
        """The first agent turn "within N exchanges of `turn`" for which `pred` is true: among the
        answers of the `exchanges` exchanges starting with the one `turn` belongs to (a user turn's
        exchange is answered by its reply; an agent turn answers its own)."""
        if exchanges < 1:
            raise ValueError("exchanges must be at least 1")
        answers = [t for t in self.turns if t.role == "agent" and t.index >= turn.index]
        return next((t for t in answers[:exchanges] if pred(t)), None)

    def tool_calls(self, name: str | None = None) -> list[tuple[Turn, ToolCall]]:
        """Every tool call in order, with the agent turn that made it; only `name`'s if given."""
        return [(t, c) for t in self.turns for c in t.tool_calls if name is None or c.name == name]

    def declared(self, **fields: Any) -> list[Turn]:
        """The user turns whose side-channel (`hidden`) holds these values."""
        return [
            t
            for t in self.turns
            if t.role == "user"
            and t.hidden is not None
            and all(key in t.hidden and t.hidden[key] == value for key, value in fields.items())
        ]

    def stimulus_turn(self, stimulus: Stimulus) -> Turn | None:
        """Where `stimulus` reached the agent. A user stimulus: the user turn that delivered it. A
        tool_result one: the first agent turn whose call to `stimulus.tool` returned a result
        containing `stimulus.marker`. None: it was never delivered."""
        if stimulus.channel == "user":
            return next(
                (t for t in self.user_turns() if t.stimulus_id == stimulus.id),
                None,
            )
        return next(
            (
                turn
                for turn, call in self.tool_calls(stimulus.tool)
                if any(stimulus.marker in s for s in json_strings(call.result))
            ),
            None,
        )


class Trial(Strict):
    schema_version: Literal[1] = 1
    run_id: str
    case_id: Id
    n: int = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    source: Literal["live", "imported"] = "live"
    target_name: Id
    exposes: Channels  # live: target.exposes + side_channel + ground_truth
    status: Literal["completed", "aborted", "error"]  # aborted = stopped by the budget
    stop_reason: Literal[
        "outcome_emitted",
        "simulator_ended",
        "max_turns",
        "tool_round_cap",
        "simulator_failed",
        "budget",
        "error",
    ]
    error: str | None = None
    fault: Literal["harness", "provider"] | None = None  # for status error
    started_at: AwareDatetime
    finished_at: AwareDatetime
    cost_usd: float = 0.0
    transcript: Transcript


# ── Grading: runs/<ts>/results.json — rebuilt by every grading; every list sorted ─────────────────


class CheckSpec(Strict):  # built by the loader from @check and pack.yaml bindings
    id: CheckId
    kind: Kind
    method: Method
    hazards: list[HazardId] = []
    controls: list[ControlId] = []
    needs: Channels = ["turn_text"]
    description: str
    origin: Literal["core", "pack"]

    @model_validator(mode="after")
    def _links_match_kind(self) -> CheckSpec:
        if self.kind == "validity" and (self.hazards or self.controls):
            raise ValueError("validity checks decide whether a trial counts; they link no hazard")
        if self.kind != "validity" and not self.hazards:
            raise ValueError("conformance and hazard checks link at least one hazard")
        return self

    @property
    def validated_oracle(self) -> bool:  # a judge becomes validated only by calibration (later)
        return self.method == "deterministic"


class _Graded(Strict):
    outcome: Outcome
    note: str = ""
    evidence_turn: int | None = None
    fault: Fault | None = None

    @model_validator(mode="after")
    def _consistent(self) -> _Graded:
        if self.fault not in FAULTS[self.outcome]:
            raise ValueError(f"fault {self.fault!r} does not fit outcome {self.outcome!r}")
        if self.outcome == "fail" and self.evidence_turn is None:
            raise ValueError("a failure points at its evidence turn")
        return self


class CheckOutcome(_Graded):  # what a check returns: pass / fail / invalid (error: core guards)
    pass


class CheckResult(_Graded):
    check_id: CheckId


class TrialGrade(Strict):
    case_id: Id
    n: int
    attempt: int
    transcript_sha256: str
    # not_run: stopped by the budget, in no denominator
    status: Literal["valid", "invalid", "error", "not_run"]
    reasons: list[str] = []  # why the trial does not count
    outcome: dict[str, Any] | None = None  # parsed final outcome, for the report
    results: list[CheckResult] = []  # kept as computed, even when the trial does not count


class CaseStats(Strict):  # one row per case (all applicable checks)
    case_id: Id
    trials: int
    valid_trials: int
    invalid_trials: int
    error_trials: int
    # valid trials with no verdict: a check errored or was invalid, or none produced a result
    unscored_trials: int
    passed_trials: int  # scored trials with no fail
    k: int  # requested repeats
    pass_at_1: float | None
    pass_hat_k: float | None  # None when scored trials < k
    # two-sided exact, over this case's scored trials (valid - unscored)
    pass_rate_interval: tuple[float, float] | None
    mixed: bool


class CaseHazardStatus(Strict):
    case_id: Id
    hazard_id: HazardId
    status: Literal["passed", "failed", "excluded"]
    reason: str = ""
    trial_statuses: list[Literal["pass", "fail", "invalid", "error", "no_evidence"]]


class EvidenceRef(Strict):
    case_id: Id
    n: int
    check_id: CheckId
    turn: int
    note: str


class HazardVerdict(Strict):
    hazard_id: HazardId
    verdict: Verdict
    reasons: list[str]  # every reason that applies, not the first one
    cases_tagged: int
    cases_counted: int  # n: passed + failed distinct cases
    cases_failed: int
    cases_excluded: int
    unstable_cases: int  # pass and fail across repeats
    observed_rate: float | None
    upper_bound: float | None
    cases_needed: int | None  # more failure-free distinct cases to reach the threshold
    unevaluable_checks: list[CheckId]  # needs not exposed by the target
    limited_evidence: bool  # no evaluable check has a validated oracle
    failures: list[EvidenceRef] = []
    # fails on cases not tagged with this hazard: shown, not counted
    outside_sample: list[EvidenceRef] = []
    # fails inside non-counted trials: shown, not counted
    invalid_trial_findings: list[EvidenceRef] = []


class LintIssue(Strict):
    code: str
    level: Literal["error", "warning"]
    where: str
    message: str


class Results(Strict):
    schema_version: Literal[1] = 1
    run_id: str
    graded_at: AwareDatetime  # from an injected clock (tests freeze it)
    graded_against: Literal["snapshot", "current"]
    pack_sha256: str
    harness_version: str
    harness_git_sha: str | None
    expectations_match: bool  # recomputed expectations == expectations.json
    checks: list[CheckSpec]
    trials: list[TrialGrade]
    cases: list[CaseStats]
    case_hazards: list[CaseHazardStatus]
    hazards: list[HazardVerdict]
    lint: list[LintIssue]


# ── Run identity: manifest.json (written once, before the first API call) + execution.json ───────


class ModelInfo(Strict):
    role: Literal["agent", "simulator", "judge"]
    env_var: str
    requested: str
    effort: Effort
    max_tokens: int


class RunManifest(Strict):  # never modified after creation
    schema_version: Literal[1] = 1
    run_id: str
    created_at: AwareDatetime
    command: list[str]
    harness_version: str
    harness_git_sha: str | None
    harness_git_dirty: bool | None
    python: str
    packages: dict[str, str]  # anthropic, pydantic, pyyaml: SOUP identification
    pack_name: str
    pack_version: str
    variant: str
    pack_sha256: str  # snapshot content hash
    acceptance_sha256: str  # canonical JSON {hazard_id: acceptance}
    target_name: str
    target_kind: str
    target_sha256: str  # target YAML + its prompt file
    system_prompt_sha256: str | None
    tools_sha256: str  # tool specs offered to the agent
    models: list[ModelInfo]
    case_ids: list[str]
    repeats: int
    max_turns: int
    seed: None = None  # the Messages API takes no seed: recorded as absent
    budget_usd: float
    estimate_usd: float
    prices_as_of: str
    judge: None = None  # reserved: judge model, rubric hashes, cache policy


class TrialIndexEntry(Strict):
    case_id: str
    n: int
    attempt: int
    file: str  # <case_id>/run_<n>.json or …/run_<n>.attempt-<k>.json
    sha256: str
    status: Literal["completed", "aborted", "error"]
    stop_reason: str
    cost_usd: float


class Execution(Strict):  # mutable progress, rewritten atomically after each trial
    run_id: str
    status: Literal["running", "completed", "aborted_budget", "aborted_error", "interrupted"]
    first_call_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    spent_usd: float = 0.0
    served_models: dict[str, list[str]] = {}  # role → distinct served ids (> 1 → report banner)
    resumed_at: list[AwareDatetime] = []
    trials: list[TrialIndexEntry] = []
