from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SuperPermission:
    """一个带密码的超级权限对象（只存加盐哈希，不存明文密码）。"""

    object_id: str
    label: str
    salt: str
    password_hash: str
    granted_at: datetime = field(default_factory=utc_now)


class SuperPermissionStore:
    """JSON 文件存储的超级权限清单，接口可替换。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._entries: dict[str, SuperPermission] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw:
            entry = SuperPermission(
                object_id=item["object_id"],
                label=item["label"],
                salt=item["salt"],
                password_hash=item["password_hash"],
                granted_at=datetime.fromisoformat(item["granted_at"]),
            )
            self._entries[entry.object_id] = entry

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "object_id": entry.object_id,
                "label": entry.label,
                "salt": entry.salt,
                "password_hash": entry.password_hash,
                "granted_at": entry.granted_at.isoformat(),
            }
            for entry in self._entries.values()
        ]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def grant(self, object_id: str, label: str, password: str) -> SuperPermission:
        password = (password or "").strip()
        if not password:
            raise ValueError("password cannot be empty")
        salt = secrets.token_hex(16)
        entry = SuperPermission(
            object_id=object_id,
            label=label,
            salt=salt,
            password_hash=_hash_password(password, salt),
        )
        self._entries[object_id] = entry
        self._save()
        return entry

    def verify(self, object_id: str, password: str) -> bool:
        entry = self._entries.get(object_id)
        if entry is None:
            return False
        return secrets.compare_digest(
            _hash_password(password or "", entry.salt),
            entry.password_hash,
        )

    def is_super(self, object_id: str) -> bool:
        return object_id in self._entries

    def revoke(self, object_id: str) -> None:
        if object_id not in self._entries:
            raise KeyError(object_id)
        del self._entries[object_id]
        self._save()

    def list(self) -> tuple[SuperPermission, ...]:
        return tuple(self._entries.values())
