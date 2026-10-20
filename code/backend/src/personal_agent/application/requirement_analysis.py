"""Requirement analysis through the single model gateway.

Chat text is analysed into candidate fields. Only values the user actually stated
enter the draft; anything inferred becomes a pending suggestion that must be
accepted explicitly. A busy or unknown model leaves the message recorded and the
operation visible instead of silently retrying.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.requirement_models import (
    ConversationMessage,
    RequirementDraft,
    Suggestion,
)
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.application.draft_operations import (
    OperationStatus,
    create_operation,
    finish_operation,
    serialize_operation,
)
from personal_agent.application.requirement_confirmation import RequirementService
from personal_agent.domain.common import (
    MODEL_BUSY,
    MODEL_OUTCOME_UNKNOWN,
    AppError,
    iso_z,
    new_id,
)
from personal_agent.domain.requirements import DraftStatus, required_paths
from personal_agent.domain.workflow_schema import WorkflowDefinition
from personal_agent.ports.contracts import (
    Clock,
    ModelGatewayPort,
    ModelMessage,
    ModelRequest,
    Owner,
)

ANALYSIS_SYSTEM_PROMPT = (
    "你是需求整理助手。只提取用户明确说出的要求，区分事实与推断；"
    "不确定的内容不要当作已确认值。不要重复询问已有答案；用户表示不知道、"
    "请你决定或没有特别偏好时，用 explicit_none 或建议表达。多个难以兼顾的喜好"
    "要用具体取舍问题引导，并给出待用户确认的优先顺序。输出 JSON："
    '{"summary": str, "fields": [{"path","value","source","rationale?","explicit_none?"}], '
    '"missing_fields": [str], "conflicts": [{"path","message"}], '
    '"suggestions": [{"field_path","value","rationale"}], "clarifying_questions": [str]}'
)


def _grounded(value: Any, text: str) -> bool:
    """A user-sourced value must be present in what the user actually wrote."""
    if isinstance(value, bool):
        return str(value).lower() in text.lower()
    if isinstance(value, (int, float)):
        return str(value) in text
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and stripped in text
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_grounded(item, text) for item in value)
    return False


class RequirementAnalysisService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        gateway: ModelGatewayPort,
        requirements: RequirementService,
        *,
        model_id: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> None:
        self._database = database
        self._clock = clock
        self._gateway = gateway
        self._requirements = requirements
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    async def analyze(self, draft_id: str, text: str) -> dict[str, Any]:
        draft, message = await self._requirements.append_message(draft_id, text)

        async with self._database.session() as session:
            operation = create_operation(
                session,
                clock=self._clock,
                draft_id=draft_id,
                message_id=message.id,
                status=OperationStatus.RUNNING,
            )
            await session.commit()
            operation_id = operation.id

        estimate = self._estimate_tokens(text)
        reservation_error: AppError | None = None
        async with self._database.session() as session:
            draft = await self._requirements.load(session, draft_id)
            if DraftStatus(draft.status) not in {DraftStatus.COLLECTING, DraftStatus.READY}:
                reservation_error = AppError(
                    code="DRAFT_STATE_INVALID",
                    message="this draft no longer accepts analysis",
                    status=409,
                )
            else:
                ledger = await session.scalar(
                    select(BudgetLedger).where(BudgetLedger.draft_id == draft_id)
                )
                if ledger is None:
                    reservation_error = AppError(
                        code="BUDGET_LEDGER_NOT_FOUND",
                        message="the draft budget ledger is missing",
                        status=503,
                    )
                else:
                    try:
                        await self._requirements.reserve_analysis_budget(
                            session,
                            ledger=ledger,
                            worst_case={"model_requests": 1, "tokens": estimate},
                            operation_id=operation_id,
                        )
                    except AppError as error:
                        reservation_error = error
            if reservation_error is not None:
                await self._finish(
                    operation_id,
                    status=OperationStatus.FAILED,
                    error=reservation_error,
                )
                return await self.operation(operation_id, draft_id=draft_id)
            await session.commit()

        async with self._database.session() as session:
            draft = await self._requirements.load(session, draft_id)
            version = await session.get(WorkflowVersion, draft.selected_version_id)
            definition = (
                WorkflowDefinition.model_validate(version.definition_json)
                if version is not None
                else None
            )
            history = list((await session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.draft_id == draft_id)
                .order_by(ConversationMessage.sequence.desc())
                .limit(12)
            )).all())
            pending = list((await session.scalars(
                select(Suggestion).where(
                    Suggestion.draft_id == draft_id, Suggestion.status == "PENDING",
                )
            )).all())
        context = {
            "workflow": definition.name if definition else "",
            "applicability": definition.applicability if definition else "",
            "required_inputs": (
                required_paths(definition.input_schema) if definition else []
            ),
            "missing_fields": list(draft.missing_fields_json),
            "confirmed_fields": dict(draft.field_values_json),
            "preference_order": list(draft.preference_order_json),
            "pending_suggestions": [
                {
                    "field_path": row.field_path,
                    "value": row.value_json,
                    "rationale": row.rationale,
                }
                for row in reversed(pending)
            ],
            "recent_messages": [
                {"role": row.role, "text": row.content_text}
                for row in reversed(history)
                if row.id != message.id
            ],
        }
        system_content = (
            ANALYSIS_SYSTEM_PROMPT
            + "\n\n当前上下文（JSON）：\n"
            + json.dumps(context, ensure_ascii=False)
        )
        spec = ModelRequest(
            request_id=operation_id,
            owner=Owner(draft_id=draft_id),
            epoch=1,
            model_id=self._model_id,
            messages=[
                ModelMessage(role="system", content=system_content),
                ModelMessage(role="user", content=text),
            ],
            output_limit=self._max_output_tokens,
            deadline=self._clock.now() + timedelta(seconds=self._timeout_seconds),
            reservation_id=operation_id,
        )
        try:
            response = await self._gateway.request(
                spec, reserved_tokens=estimate, timeout_seconds=self._timeout_seconds
            )
        except AppError as error:
            # The request never started (busy or unknown blocker): release the
            # local reservation and keep the message + operation visible.
            await self._release_reservation(operation_id, draft_id, estimate)
            status = (
                OperationStatus.UNKNOWN
                if error.code in {MODEL_OUTCOME_UNKNOWN}
                else OperationStatus.WAITING_MODEL
                if error.code == MODEL_BUSY
                else OperationStatus.FAILED
            )
            await self._finish(operation_id, status=status, error=error)
            return await self.operation(operation_id, draft_id=draft_id)

        if response.status != "completed" or response.content is None:
            status = (
                OperationStatus.UNKNOWN
                if response.status == "unknown"
                else OperationStatus.FAILED
            )
            await self._finish(
                operation_id,
                status=status,
                error=response.error
                or AppError(
                    code="ANALYSIS_INCOMPLETE",
                    message="the analysis did not return a usable result",
                    status=409,
                ),
            )
            return await self.operation(operation_id, draft_id=draft_id)

        try:
            payload = json.loads(response.content)
            if not isinstance(payload, dict):
                raise ValueError("analysis output must be an object")
            payload = self._validated_payload(payload)
        except ValueError as error:
            await self._settle(operation_id, draft_id, estimate, response.usage.tokens)
            await self._finish(
                operation_id,
                status=OperationStatus.FAILED,
                error=AppError(
                    code="ANALYSIS_INVALID_OUTPUT",
                    message="the analysis output did not match the expected shape",
                    status=422,
                    details={"reason": type(error).__name__},
                ),
            )
            return await self.operation(operation_id, draft_id=draft_id)

        await self._apply(draft_id, payload, message)
        await self._settle(operation_id, draft_id, estimate, response.usage.tokens)
        async with self._database.session() as session:
            draft = await self._requirements.load(session, draft_id)
            result_ref = f"draft:{draft_id}:{draft.summary_hash}"
        await self._finish(operation_id, status=OperationStatus.SUCCEEDED, result_ref=result_ref)
        return await self.operation(operation_id, draft_id=draft_id)

    async def operation(self, operation_id: str, *, draft_id: str | None = None) -> dict[str, Any]:
        from personal_agent.adapters.persistence.operation_models import AnalysisOperation

        async with self._database.session() as session:
            operation = await session.get(AnalysisOperation, operation_id)
            if operation is None:
                raise AppError(
                    code="OPERATION_NOT_FOUND", message="operation not found", status=404
                )
            if draft_id is not None and operation.draft_id != draft_id:
                raise AppError(
                    code="OPERATION_NOT_FOUND", message="operation not found", status=404
                )
            return serialize_operation(operation)

    # ------------------------------------------------------------------
    @staticmethod
    def _validated_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Enforce the strict analysis output contract before touching the draft."""
        fields = payload.get("fields")
        if fields is not None and not isinstance(fields, list):
            raise ValueError("fields must be a list")
        for item in fields or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("each field needs a path")
            if "value" not in item:
                raise ValueError("each field needs a value")
            if str(item.get("source", "user")) not in {"user", "suggestion"}:
                raise ValueError("field source must be user or suggestion")
        for key in ("suggestions", "conflicts", "clarifying_questions", "missing_fields"):
            value = payload.get(key)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
        for item in payload.get("suggestions") or []:
            if not isinstance(item, dict) or not isinstance(item.get("field_path"), str):
                raise ValueError("each suggestion needs a field_path")
        for item in payload.get("conflicts") or []:
            if not isinstance(item, dict) or "path" not in item:
                raise ValueError("each conflict needs a path")
        preference = payload.get("preference_order")
        if preference is not None and not isinstance(preference, list):
            raise ValueError("preference_order must be a list")
        output_spec = payload.get("output_spec")
        if output_spec is not None and not isinstance(output_spec, dict):
            raise ValueError("output_spec must be an object")
        return payload

    async def _apply(
        self,
        draft_id: str,
        payload: dict[str, Any],
        message: ConversationMessage,
    ) -> None:
        async with self._database.session() as session:
            newer = await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.draft_id == draft_id,
                    ConversationMessage.sequence > message.sequence,
                )
            )
        if newer:
            # A later message already advanced the draft; a late analysis result
            # must not overwrite the newer state or a consumed snapshot.
            return
        async with self._database.session() as session:
            draft = await self._requirements.load(session, draft_id)
            version = await session.get(WorkflowVersion, draft.selected_version_id)
            if version is None:
                return
            definition = WorkflowDefinition.model_validate(version.definition_json)
            fields = dict(draft.field_values_json)
            for item in payload.get("fields") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                evidence = str(item.get("evidence") or "").strip()
                if str(item.get("source", "user")) == "user" and (
                    evidence or _grounded(item.get("value"), message.content_text)
                ):
                    fields[path] = {
                        "path": path,
                        "value": item.get("value"),
                        "source": "user",
                        "message_id": message.id,
                        "suggestion_id": None,
                        "accepted_at": iso_z(self._clock.now()),
                        "explicit_none": bool(item.get("explicit_none", False)),
                    }
                else:
                    # A model claim of source=user is not enough: ungrounded values
                    # stay pending suggestions and never silently enter the draft.
                    session.add(Suggestion(
                        id=new_id(),
                        draft_id=draft_id,
                        field_path=path,
                        value_json=item.get("value"),
                        rationale=str(item.get("rationale", "模型推断，待用户确认")),
                        status="PENDING",
                        accepted_message_id=None,
                        created_at=self._clock.now(),
                        revision=1,
                    ))
            draft.field_values_json = fields
            if isinstance(payload.get("preference_order"), list):
                draft.preference_order_json = [
                    str(item) for item in payload["preference_order"]
                ]
            if isinstance(payload.get("output_spec"), dict):
                draft.output_spec_json = dict(payload["output_spec"])
            for item in payload.get("suggestions") or []:
                if not isinstance(item, dict):
                    continue
                session.add(
                    Suggestion(
                        id=new_id(),
                        draft_id=draft_id,
                        field_path=str(item.get("field_path", "")),
                        value_json=item.get("value"),
                        rationale=str(item.get("rationale", "")),
                        status="PENDING",
                        accepted_message_id=None,
                        created_at=self._clock.now(),
                        revision=1,
                    )
                )
            draft.conflicts_json = [
                dict(item)
                for item in (payload.get("conflicts") or [])
                if isinstance(item, dict)
            ]
            # Persist the assistant's questions and rationale so the next turn can
            # avoid repeating answered questions and the user can review them.
            summary = str(payload.get("summary") or "").strip()
            questions = [
                str(question).strip()
                for question in payload.get("clarifying_questions") or []
                if str(question).strip()
            ]
            if summary or questions:
                sequence = int(
                    await session.scalar(
                        select(func.max(ConversationMessage.sequence)).where(
                            ConversationMessage.draft_id == draft_id
                        )
                    )
                    or 0
                ) + 1
                body = summary + (
                    "\n\n" + "\n".join(f"- {question}" for question in questions)
                    if questions else ""
                )
                session.add(ConversationMessage(
                    id=new_id(),
                    draft_id=draft_id,
                    sequence=sequence,
                    role="assistant",
                    content_ref=f"analysis:{message.id}",
                    content_text=body,
                    created_at=self._clock.now(),
                ))
            draft.revision += 1
            await self._requirements.recompute(session, draft, definition)
            await session.commit()

    async def _finish(
        self,
        operation_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        error: AppError | None = None,
    ) -> None:
        from personal_agent.adapters.persistence.operation_models import AnalysisOperation

        async with self._database.session() as session:
            operation = await session.get(AnalysisOperation, operation_id)
            if operation is None:
                return
            finish_operation(
                operation, clock=self._clock, status=status, result_ref=result_ref, error=error
            )
            await session.commit()

    async def _settle(
        self, operation_id: str, draft_id: str, reserved: int, actual: int
    ) -> None:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.draft_id == draft_id)
            )
            if ledger is None:
                return
            await self._requirements.settle_analysis_budget(
                session,
                ledger=ledger,
                reserved={"model_requests": 1, "tokens": reserved},
                actual={"model_requests": 1, "tokens": actual},
                operation_id=operation_id,
            )
            await session.commit()

    async def _release_reservation(
        self, operation_id: str, draft_id: str, reserved: int
    ) -> None:
        async with self._database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.draft_id == draft_id)
            )
            if ledger is None:
                return
            await self._requirements.release_analysis_budget(
                session,
                ledger=ledger,
                reserved={"model_requests": 1, "tokens": reserved},
                operation_id=operation_id,
            )
            await session.commit()

    def _estimate_tokens(self, text: str) -> int:
        # Conservative upper bound: CJK characters are roughly one token each,
        # plus the full output allowance so a call never starts under-reserved.
        input_bound = len(text) * 2 + 64
        return input_bound + self._max_output_tokens


async def touch_draft(session: Any, draft: RequirementDraft, *, revision: int) -> None:
    await cas_update(
        session,
        RequirementDraft,
        identity={"id": draft.id},
        expected_revision=revision,
        values={"updated_at": draft.updated_at},
        stale_code="DRAFT_REVISION_STALE",
    )
