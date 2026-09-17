"""205 策划时现读现场正文。不写入交接或记挂。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .intake import IntakeRecord

logger = logging.getLogger(__name__)


def bind_scene_loader(active_zone: Any, zone_store: Any) -> Callable[[IntakeRecord], str]:
    """按交接里的 zone_kind 读木头活跃区或人格片场。读失败则空串，不挡策划。"""

    def load(intake: IntakeRecord) -> str:
        subject_id = (intake.subject_id or "").strip()
        if not subject_id:
            return ""
        kind = str((intake.field_ref or {}).get("zone_kind") or "").strip() or "wood"
        try:
            if kind == "persona" and zone_store is not None:
                render = getattr(zone_store, "render", None)
                if callable(render):
                    return str(render(subject_id) or "").strip()
                return ""
            if active_zone is None:
                return ""
            view = active_zone.load(subject_id)
            return str(getattr(view, "context_text", "") or "").strip()
        except Exception:
            logger.exception("205 现读现场失败")
            return ""

    return load
