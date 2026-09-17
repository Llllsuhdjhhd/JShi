from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from jshi.core.params import VALUE_CATALOG_REFRESH_SECONDS
from jshi.subject.domain import (
    HistoryKind,
    HistoryRecord,
    PersonalItem,
    PersonalKind,
    PersonalStatus,
    StateTransition,
    new_id,
    utc_now,
)
from jshi.subject.repository import SubjectRepository

from .store import SqliteValueStore, ValueStore


class ValueSource(StrEnum):
    """100 的价值观/边界来源。"""

    CLASSIC_WORK = "classic_work"
    CLASSIC_NOVEL = "classic_novel"
    MODEL_PROPOSAL = "model_proposal"
    HUMAN_INTERVENTION = "human_intervention"
    DERIVED_EXPERIENCE = "derived_experience"
    GOVERNANCE_REVIEW = "governance_review"


VALUE_ROLES = frozenset({"value", "boundary"})
LOADABLE_VALUE_STATUSES = frozenset(
    {
        PersonalStatus.ACTIVE,
        PersonalStatus.ACCEPTED,
        PersonalStatus.LOCKED,
    }
)
CONSOLIDATION_STATUSES = frozenset(
    {
        PersonalStatus.CANDIDATE,
        PersonalStatus.ACCEPTED,
    }
)
IMPORTABLE_STATUSES = frozenset(
    {
        PersonalStatus.CANDIDATE,
        PersonalStatus.ACCEPTED,
        PersonalStatus.LOCKED,
    }
)


def item_importance(item: PersonalItem) -> float:
    """个人条目的重要程度；暂存于 metadata，未来升格为正式字段。"""
    return float(item.metadata.get("importance", 1.0))


def is_boundary(item: PersonalItem) -> bool:
    """边界实例使用 metadata.role="boundary" 表达，暂不新增 PersonalKind。"""
    return item.metadata.get("role") == "boundary"


def is_binding(item: PersonalItem) -> bool:
    """binding 边界需要常驻约束；缺省视为 binding。"""
    return bool(item.metadata.get("binding", True))


def is_loadable_value(item: PersonalItem) -> bool:
    """价值/边界能否进入当前状态：active/accepted/locked 可装载。"""
    return (
        item.kind is PersonalKind.VALUE
        and item.status in LOADABLE_VALUE_STATUSES
    )


@dataclass(frozen=True)
class ValueImportReport:
    """JSON 批量导入结果。"""

    imported_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValueConsolidationSuggestion:
    """自省整理输出的建议，不直接覆盖原始实例。"""

    type: str  # merge | abstract
    item_ids: tuple[str, ...]
    reason: str
    keep_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValueConsolidationReport:
    """一次自省运行的结果；cursor 用于幂等续跑。"""

    subject_id: str
    run_id: str
    processed_count: int
    total_count: int
    suggestions: tuple[ValueConsolidationSuggestion, ...]
    cursor: int


class ValuesPort(Protocol):
    """100 价值观与边界的完整接口。"""

    def select_values(
        self, subject_id: str, context: str, *, budget: int = 4
    ) -> Sequence[PersonalItem]: ...

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]: ...

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]: ...

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]: ...

    def propose_value(
        self,
        subject_id: str,
        content: str,
        *,
        summary: str = "",
        stance: str = "",
        role: str = "value",
        source_type: ValueSource,
        source_id: str | None = None,
        domains: Sequence[str] = (),
        context_tags: Sequence[str] = (),
        importance: float = 1.0,
        binding: bool | None = None,
        source_ids: Sequence[str] = (),
    ) -> PersonalItem: ...

    def review_value(
        self,
        item_id: str,
        decision: str,
        reason: str,
        reviewer: str,
    ) -> PersonalItem: ...

    def lock_value(
        self,
        item_id: str,
        reason: str,
        approver: str,
    ) -> PersonalItem: ...

    def supersede_value(
        self,
        item_id: str,
        replacement_id: str,
        reason: str,
    ) -> PersonalItem: ...

    def import_entries(
        self,
        subject_id: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> ValueImportReport: ...

    def export_document(
        self,
        subject_id: str,
    ) -> Mapping[str, Any]: ...

    def consolidate(
        self,
        subject_id: str,
        budget: int = 20,
        cursor: int = 0,
    ) -> ValueConsolidationReport: ...

    def mark_used(
        self,
        item_id: str,
        at: datetime | None = None,
    ) -> PersonalItem: ...


class InProcessValues:
    """100 完整本地实现：价值写入独立 ValueStore，审计仍记在主体仓库。"""

    def __init__(
        self,
        repository: SubjectRepository,
        store: ValueStore | None = None,
        *,
        refresh_seconds: float | None = None,
    ) -> None:
        self._repository = repository
        self._store = store or SqliteValueStore(repository.path)
        self._refresh_seconds = (
            VALUE_CATALOG_REFRESH_SECONDS
            if refresh_seconds is None
            else float(refresh_seconds)
        )

    def _all_values(self, subject_id: str) -> Sequence[PersonalItem]:
        return self._store.list(subject_id)

    def get(self, entry_id: str) -> PersonalItem | None:
        return self._store.get(entry_id)

    def should_reload_values(
        self, subject_id: str, *, now: datetime | None = None
    ) -> bool:
        gate = self._store.load_gate(subject_id)
        if gate.last_loaded_revision < gate.catalog_revision:
            return True
        if gate.last_loaded_at is None:
            return True
        if self._refresh_seconds <= 0:
            return False
        stamp = now or utc_now()
        elapsed = (stamp - gate.last_loaded_at).total_seconds()
        return elapsed >= self._refresh_seconds

    def hydrate_loaded_values(self, subject_id: str) -> Sequence[PersonalItem]:
        items: list[PersonalItem] = []
        for entry_id in self._store.load_gate(subject_id).loaded_ids:
            item = self._store.get(entry_id)
            if item is None or is_boundary(item) or not is_loadable_value(item):
                continue
            items.append(item)
        return tuple(items)

    def mark_catalog_loaded(
        self, subject_id: str, loaded_ids: Sequence[str]
    ) -> None:
        self._store.mark_loaded(subject_id, loaded_ids=loaded_ids)

    def _bump(self, subject_id: str) -> None:
        self._store.bump_catalog(subject_id)

    def _loadable_values(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(
            item for item in self._all_values(subject_id) if is_loadable_value(item)
        )

    # ------------------------------------------------------------------
    # 装载侧
    # ------------------------------------------------------------------

    def select_values(
        self, subject_id: str, context: str, *, budget: int = 4
    ) -> Sequence[PersonalItem]:
        del context
        return tuple(self.list_ordered(subject_id)[: max(budget, 0)])

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(
            item
            for item in self._loadable_values(subject_id)
            if not is_boundary(item)
        )

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(
            item
            for item in self._loadable_values(subject_id)
            if is_boundary(item) and is_binding(item)
        )

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        return self.select_boundaries(subject_id)

    # ------------------------------------------------------------------
    # 窄写入侧
    # ------------------------------------------------------------------

    def propose_value(
        self,
        subject_id: str,
        content: str,
        *,
        summary: str = "",
        stance: str = "",
        role: str = "value",
        source_type: ValueSource,
        source_id: str | None = None,
        domains: Sequence[str] = (),
        context_tags: Sequence[str] = (),
        importance: float = 1.0,
        binding: bool | None = None,
        source_ids: Sequence[str] = (),
    ) -> PersonalItem:
        self._validate_role(role)
        if not isinstance(source_type, ValueSource):
            raise ValueError(f"Invalid value source: {source_type}")

        metadata: dict[str, Any] = {
            "role": role,
            "source_type": source_type.value,
            "summary": summary,
            "stance": stance,
            "domains": tuple(domains),
            "context_tags": tuple(context_tags),
            "importance": float(importance),
        }
        if source_id is not None:
            metadata["source_id"] = str(source_id)
        if role == "boundary":
            metadata["binding"] = True if binding is None else bool(binding)

        item = PersonalItem(
            subject_id=subject_id,
            kind=PersonalKind.VALUE,
            content=content,
            source_ids=tuple(source_ids),
            status=PersonalStatus.CANDIDATE,
            metadata=metadata,
        )
        self._store.put(item)
        self._bump(subject_id)
        self._repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_proposed",
                content={
                    "personal_item_id": item.id,
                    "role": role,
                    "summary": summary,
                    "content": content,
                    "source_type": source_type.value,
                    "source_id": source_id,
                },
                source_ids=tuple(source_ids),
            )
        )
        return item

    def review_value(
        self,
        item_id: str,
        decision: str,
        reason: str,
        reviewer: str,
    ) -> PersonalItem:
        current = self._require_value(item_id)
        if current.status is not PersonalStatus.CANDIDATE:
            raise ValueError(
                f"Only candidate values can be reviewed; current={current.status.value}"
            )
        if decision == "accept":
            target = PersonalStatus.ACCEPTED
        elif decision == "reject":
            target = PersonalStatus.REJECTED
        else:
            raise ValueError(f"Invalid review decision: {decision}")

        updated = self._transition(current, target, reason)
        self._bump(current.subject_id)
        self._repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_reviewed",
                content={
                    "personal_item_id": item_id,
                    "decision": decision,
                    "to": target.value,
                    "reason": reason,
                    "reviewer": reviewer,
                },
                source_ids=(item_id,),
            )
        )
        return updated

    def lock_value(
        self,
        item_id: str,
        reason: str,
        approver: str,
    ) -> PersonalItem:
        current = self._require_value(item_id)
        if current.status not in {PersonalStatus.ACCEPTED, PersonalStatus.ACTIVE}:
            raise ValueError(
                f"Only accepted/active values can be locked; current={current.status.value}"
            )
        updated = self._transition(current, PersonalStatus.LOCKED, reason)
        self._bump(current.subject_id)
        self._repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_locked",
                content={
                    "personal_item_id": item_id,
                    "reason": reason,
                    "approver": approver,
                },
                source_ids=(item_id,),
            )
        )
        return updated

    def supersede_value(
        self,
        item_id: str,
        replacement_id: str,
        reason: str,
    ) -> PersonalItem:
        current = self._require_value(item_id)
        if current.status not in {
            PersonalStatus.ACCEPTED,
            PersonalStatus.LOCKED,
            PersonalStatus.ACTIVE,
        }:
            raise ValueError(
                f"Value cannot be superseded from {current.status.value}"
            )
        replacement = self._store.get(replacement_id)
        if replacement is None:
            raise KeyError(replacement_id)
        if replacement.subject_id != current.subject_id:
            raise ValueError("Replacement value belongs to a different subject")
        updated = self._transition(current, PersonalStatus.SUPERSEDED, reason)
        self._bump(current.subject_id)
        self._repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_superseded",
                content={
                    "personal_item_id": item_id,
                    "replacement_id": replacement_id,
                    "reason": reason,
                },
                source_ids=(item_id, replacement_id),
            )
        )
        return updated

    # ------------------------------------------------------------------
    # JSON 导入/导出
    # ------------------------------------------------------------------

    def import_entries(
        self,
        subject_id: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> ValueImportReport:
        imported: list[str] = []
        skipped: list[str] = []
        for entry in entries:
            try:
                item = self._entry_to_item(subject_id, entry)
            except ValueError:
                entry_id = str(entry.get("id", "<missing>"))
                skipped.append(entry_id)
                continue
            existing = self._store.get(item.id)
            if existing is not None:
                skipped.append(item.id)
                continue
            self._store.put(item)
            imported.append(item.id)
        if imported:
            self._bump(subject_id)
        self._repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_imported",
                content={
                    "imported_ids": imported,
                    "skipped_ids": skipped,
                },
            )
        )
        return ValueImportReport(
            imported_ids=tuple(imported),
            skipped_ids=tuple(skipped),
        )

    def export_document(self, subject_id: str) -> Mapping[str, Any]:
        entries = [
            _item_to_entry(item) for item in self._all_values(subject_id)
        ]
        return {
            "schema_version": 1,
            "subject_id": subject_id,
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # 自省整理
    # ------------------------------------------------------------------

    def consolidate(
        self,
        subject_id: str,
        budget: int = 20,
        cursor: int = 0,
    ) -> ValueConsolidationReport:
        items = sorted(
            (
                item
                for item in self._all_values(subject_id)
                if item.status in CONSOLIDATION_STATUSES
            ),
            key=lambda item: (item.created_at, item.id),
        )
        window = items[max(cursor, 0) :]
        suggestions = self._suggest(window)[: max(budget, 0)]

        run_id = new_id()
        next_cursor = min(len(items), max(cursor, 0) + len(window))
        report = ValueConsolidationReport(
            subject_id=subject_id,
            run_id=run_id,
            processed_count=len(window),
            total_count=len(items),
            suggestions=tuple(suggestions),
            cursor=next_cursor,
        )
        self._repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="value_consolidation_run",
                content={
                    "run_id": run_id,
                    "processed_count": report.processed_count,
                    "total_count": report.total_count,
                    "suggestions": [
                        {
                            "type": suggestion.type,
                            "item_ids": list(suggestion.item_ids),
                            "reason": suggestion.reason,
                            "keep_id": suggestion.keep_id,
                        }
                        for suggestion in report.suggestions
                    ],
                    "budget": budget,
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                },
            )
        )
        return report

    def mark_used(
        self,
        item_id: str,
        at: datetime | None = None,
    ) -> PersonalItem:
        item = self._require_value(item_id)
        count = int(item.metadata.get("use_count", 0)) + 1
        return self._store.update_metadata(
            item_id,
            {
                "last_used_at": (at or utc_now()).isoformat(),
                "use_count": count,
            },
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _entry_to_item(
        self,
        subject_id: str,
        entry: Mapping[str, Any],
    ) -> PersonalItem:
        role = str(entry.get("role", "value"))
        self._validate_role(role)
        source_type = ValueSource(str(entry.get("source_type")))
        status_raw = entry.get("status", "candidate")
        try:
            status = PersonalStatus(str(status_raw))
        except ValueError as exc:
            raise ValueError(f"Invalid status: {status_raw}") from exc
        if status not in IMPORTABLE_STATUSES:
            raise ValueError(f"Status not importable: {status_raw}")

        metadata: dict[str, Any] = {
            "role": role,
            "source_type": source_type.value,
            "summary": str(entry.get("summary", "")),
            "stance": str(entry.get("stance", "")),
            "domains": tuple(entry.get("domains", ())),
            "context_tags": tuple(entry.get("context_tags", ())),
            "importance": float(entry.get("importance", 1.0)),
        }
        if entry.get("source_id") is not None:
            metadata["source_id"] = str(entry["source_id"])
        if role == "boundary":
            metadata["binding"] = bool(entry.get("binding", True))

        item_id = str(entry["id"]) if entry.get("id") else new_id()
        revision = int(entry.get("version", 1))
        return PersonalItem(
            id=item_id,
            subject_id=subject_id,
            kind=PersonalKind.VALUE,
            content=str(entry.get("content", "")),
            source_ids=tuple(entry.get("source_ids", ())),
            status=status,
            metadata=metadata,
            revision=revision,
        )

    def _require_value(self, item_id: str) -> PersonalItem:
        item = self._store.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if item.kind is not PersonalKind.VALUE:
            raise ValueError(f"Item {item_id} is not a value item")
        return item

    def _transition(
        self,
        item: PersonalItem,
        status: PersonalStatus,
        reason: str,
    ) -> PersonalItem:
        updated = self._store.update_status(item.id, status)
        self._repository.add_transition(
            StateTransition(
                subject_id=item.subject_id,
                target_type="personal_item",
                target_id=item.id,
                from_state=item.status.value,
                to_state=status.value,
                reason=reason,
            )
        )
        return updated

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in VALUE_ROLES:
            raise ValueError(f"Invalid value role: {role}")

    @staticmethod
    def _suggest(
        items: Sequence[PersonalItem],
    ) -> list[ValueConsolidationSuggestion]:
        suggestions: list[ValueConsolidationSuggestion] = []
        grouped: dict[tuple[str, ...], list[PersonalItem]] = {}
        for item in items:
            domains = tuple(item.metadata.get("domains", ()))
            grouped.setdefault(domains, []).append(item)

        for domain, group in grouped.items():
            seen: dict[str, list[PersonalItem]] = {}
            for item in group:
                key = _normalize(item.content)
                seen.setdefault(key, []).append(item)
            for duplicates in seen.values():
                if len(duplicates) > 1:
                    keep = min(
                        duplicates,
                        key=lambda item: (item.created_at, item.id),
                    )
                    suggestions.append(
                        ValueConsolidationSuggestion(
                            type="merge",
                            item_ids=tuple(item.id for item in duplicates),
                            reason="duplicate_content",
                            keep_id=keep.id,
                            detail={"domains": domain},
                        )
                    )
            for item in group:
                if not item.metadata.get("domains") and not item.metadata.get(
                    "context_tags"
                ):
                    suggestions.append(
                        ValueConsolidationSuggestion(
                            type="abstract",
                            item_ids=(item.id,),
                            reason="missing_domain_and_context",
                        )
                    )
        return suggestions


def _normalize(content: str) -> str:
    return " ".join(content.strip().lower().split())


def _item_to_entry(item: PersonalItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "role": item.metadata.get("role", "value"),
        "version": item.revision,
        "summary": item.metadata.get("summary", ""),
        "content": item.content,
        "stance": item.metadata.get("stance", ""),
        "source_type": item.metadata.get("source_type"),
        "source_id": item.metadata.get("source_id"),
        "source_ids": list(item.source_ids),
        "domains": list(item.metadata.get("domains", ())),
        "context_tags": list(item.metadata.get("context_tags", ())),
        "importance": item_importance(item),
        "status": item.status.value,
        "binding": item.metadata.get("binding"),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "last_used_at": item.metadata.get("last_used_at"),
        "use_count": item.metadata.get("use_count", 0),
    }
