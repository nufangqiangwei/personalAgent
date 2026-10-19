"""Workflow definition contract v1 (Pydantic 2020-12 models).

The JSON document is the persisted format; semantic rules (reachability, cycles,
transitions, tool policy, expression bounds) live in ``workflow_validation``. Every
model forbids unknown fields so a typo cannot silently become part of a published
version.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
DIRECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")

MAX_STEPS = 100
MAX_NAME = 200
MAX_DESCRIPTION = 4000
MAX_APPLICABILITY = 4000
MAX_SCHEMA_DEPTH = 20
MAX_EXPRESSION_DEPTH = 10
MAX_EXPRESSION_NODES = 100


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Binding(StrictModel):
    source: Literal["input", "step"]
    step_id: str | None = None
    pointer: str

    @model_validator(mode="after")
    def _step_binding_needs_step(self) -> Binding:
        if self.source == "step" and not self.step_id:
            raise ValueError("a step binding requires step_id")
        if self.source == "input" and self.step_id:
            raise ValueError("an input binding must not name a step")
        return self


class Direction(StrictModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=2000)

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not DIRECTION_ID_PATTERN.match(value):
            raise ValueError("direction id must use letters, digits, '_' or '-' (1-64)")
        return value


class StepPolicy(StrictModel):
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    max_attempts: int = Field(ge=1, le=3, default=3)
    timeout_seconds: int = Field(ge=1, le=3600, default=120)
    side_effect_mode: Literal["none", "managed_artifact"] = "none"


class AgentTaskConfig(StrictModel):
    goal_template: str = Field(min_length=1, max_length=4000)
    context_bindings: list[str] = Field(default_factory=list)
    direction_ids: list[str] = Field(default_factory=list)


class ToolTaskConfig(StrictModel):
    tool_name: str = Field(min_length=1, max_length=100)
    arguments_bindings: dict[str, str] = Field(default_factory=dict)
    direction_id: str | None = None


class Operand(StrictModel):
    literal: str | int | float | bool | None = None
    ref: Binding | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Operand:
        present = [self.literal is not None, self.ref is not None]
        if sum(present) != 1:
            raise ValueError("an operand needs exactly one of literal or ref")
        return self


class Expression(StrictModel):
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "and", "or", "not"]
    left: Operand | None = None
    right: Operand | None = None
    args: list[Expression] = Field(default_factory=list)
    arg: Expression | None = None

    @model_validator(mode="after")
    def _shape(self) -> Expression:
        if self.op in {"and", "or"}:
            if not self.args or self.left or self.right or self.arg:
                raise ValueError("and/or take only a non-empty args list")
        elif self.op == "not":
            if self.arg is None or self.args or self.left or self.right:
                raise ValueError("not takes only its arg")
        else:
            if self.left is None or self.right is None or self.args or self.arg:
                raise ValueError("comparison operators take left and right operands")
        return self


class ConditionConfig(StrictModel):
    expression: Expression


class HumanReviewConfig(StrictModel):
    prompt: str = Field(min_length=1, max_length=4000)
    decision_schema: dict = Field(default_factory=dict)
    reason_code: str = Field(min_length=1, max_length=64)


class TransformConfig(StrictModel):
    transform_id: str = Field(min_length=1, max_length=64)
    parameters: dict = Field(default_factory=dict)


class OutputConfig(StrictModel):
    renderer: Literal["markdown"]
    structure: Literal["report", "checklist", "comparison"]
    content_bindings: list[str] = Field(default_factory=list)


Config = (
    AgentTaskConfig
    | ToolTaskConfig
    | ConditionConfig
    | HumanReviewConfig
    | TransformConfig
    | OutputConfig
)


class Step(StrictModel):
    id: str
    type: Literal["AgentTask", "ToolTask", "Condition", "HumanReview", "Transform", "Output"]
    name: str = Field(min_length=1, max_length=200)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    input_reads: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    policy: StepPolicy = Field(default_factory=StepPolicy)
    config: Config

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not STEP_ID_PATTERN.match(value):
            raise ValueError("step id must use letters, digits or '_' (1-64)")
        return value

    @model_validator(mode="after")
    def _config_matches_type(self) -> Step:
        expected = {
            "AgentTask": AgentTaskConfig,
            "ToolTask": ToolTaskConfig,
            "Condition": ConditionConfig,
            "HumanReview": HumanReviewConfig,
            "Transform": TransformConfig,
            "Output": OutputConfig,
        }[self.type]
        if not isinstance(self.config, expected):
            raise ValueError(f"{self.type} requires its matching config")
        return self


class Transition(StrictModel):
    from_: str = Field(alias="from")
    to: str
    on: Literal["success", "true", "false", "exhausted"]


class ExecutionPolicy(StrictModel):
    max_active_seconds: int = Field(ge=1, le=86_400, default=1200)
    max_model_requests: int = Field(ge=1, le=1000, default=30)
    max_tool_calls: int = Field(ge=1, le=1000, default=60)
    max_search_calls: int = Field(ge=0, le=1000, default=20)
    max_page_reads: int = Field(ge=0, le=1000, default=40)
    max_tokens: int = Field(ge=1, le=1_000_000, default=100_000)
    max_attempts_per_step: int = Field(ge=1, le=3, default=3)
    model_timeout_seconds: int = Field(ge=1, le=600, default=120)
    tool_timeout_seconds: int = Field(ge=1, le=600, default=120)


class CompletionPolicy(StrictModel):
    required_output_step_ids: list[str] = Field(min_length=1)
    required_direction_ids: list[str] = Field(default_factory=list)
    allow_partial_document: bool = False


class WorkflowDefinition(StrictModel):
    schema_version: Literal["1"] = "1"
    workflow_id: str
    version: int = Field(ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION)
    applicability: str = Field(min_length=1, max_length=MAX_APPLICABILITY)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    entry_step: str
    directions: list[Direction] = Field(min_length=1)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    transition_rules: list[Transition] = Field(default_factory=list)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    completion_policy: CompletionPolicy

    @field_validator("workflow_id")
    @classmethod
    def _uuid_like(cls, value: str) -> str:
        import uuid

        uuid.UUID(value)
        return value


JsonScalar = str | int | float | bool | None
AnnotatedScalar = Annotated[JsonScalar, Field(union_mode="left_to_right")]


def schema_depth(schema: object, depth: int = 0) -> int:
    """Maximum nesting depth of a JSON Schema document."""
    if depth > MAX_SCHEMA_DEPTH + 1:
        return depth
    if isinstance(schema, dict):
        return max(
            [depth]
            + [schema_depth(value, depth + 1) for value in schema.values()]
            + [schema_depth(item, depth + 1) for item in schema.values() if isinstance(item, list)]
        )
    if isinstance(schema, list):
        return max([depth] + [schema_depth(item, depth + 1) for item in schema])
    return depth


def expression_stats(expression: Expression) -> tuple[int, int]:
    """Return ``(depth, node_count)`` for a parsed expression tree."""
    if expression.op in {"and", "or"}:
        children = [expression_stats(child) for child in expression.args]
    elif expression.op == "not":
        children = [expression_stats(expression.arg)] if expression.arg else []
    else:
        children = []
    depth = 1 + max((child[0] for child in children), default=0)
    nodes = 1 + sum(child[1] for child in children)
    return depth, nodes

