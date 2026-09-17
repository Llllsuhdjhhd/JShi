from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class PromptRule:
    """一条由超级权限用户写入提示词的附加规则。"""

    id: str
    subject_id: str
    section: str  # general 或某个提示词区块名
    content: str
    source: str
    created_at: datetime = field(default_factory=utc_now)


class PromptRuleStore:
    """JSON 文件存储的提示词附加规则，接口可替换。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._rules: list[PromptRule] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw:
            self._rules.append(
                PromptRule(
                    id=item["id"],
                    subject_id=item["subject_id"],
                    section=item.get("section") or "general",
                    content=item["content"],
                    source=item["source"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "id": rule.id,
                "subject_id": rule.subject_id,
                "section": rule.section,
                "content": rule.content,
                "source": rule.source,
                "created_at": rule.created_at.isoformat(),
            }
            for rule in self._rules
        ]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def add(
        self,
        subject_id: str,
        content: str,
        *,
        section: str = "general",
        source: str = "",
    ) -> PromptRule:
        content = (content or "").strip()
        if not content:
            raise ValueError("rule content cannot be empty")
        rule = PromptRule(
            id=new_id(),
            subject_id=subject_id,
            section=(section or "general").strip() or "general",
            content=content,
            source=source,
        )
        self._rules.append(rule)
        self._save()
        return rule

    def list(self, subject_id: str) -> tuple[PromptRule, ...]:
        return tuple(rule for rule in self._rules if rule.subject_id == subject_id)

    def remove(self, rule_id: str) -> None:
        for index, rule in enumerate(self._rules):
            if rule.id == rule_id:
                del self._rules[index]
                self._save()
                return
        raise KeyError(rule_id)
