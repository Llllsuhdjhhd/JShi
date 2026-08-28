"""对话会话：斜杠命令与 experience 调用。不依赖 Textual。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jshi.recognition import CarrierEntry

SESSION_FILE = "cli_session.json"

# (命令, 参数, 说明)。全屏壳用同一份列表做箭头选择。
TALK_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/speaker", "名字", "切换对象（写入会话，下次启动仍有效）"),
    ("/who", "", "当前对象"),
    ("/context", "", "看本轮活跃区与装载（不调模型）"),
    ("/plan", "", "看上一轮 05 的 response_plan 条目"),
    ("/prompt", "", "看认知 skill 提示词骨架（本轮材料用 /context）"),
    ("/quit", "", "结束"),
)


def format_help_text() -> str:
    lines = []
    for name, argument, summary in TALK_COMMANDS:
        usage = f"{name} {argument}".strip() if argument else name
        lines.append(f"{usage:<16} {summary}")
    lines.append(
        "价值观不在对话里改：CLI 的 import-values / propose-value / "
        "review-value / lock-value / values"
    )
    return "\n".join(lines)


HELP_TEXT = format_help_text()

EventKind = Literal["speech", "notice", "meta", "overlay"]


class TalkSetupError(Exception):
    """启动对话前的配置错误（缺主体、缺说话人等）。"""


@dataclass(frozen=True)
class TalkEvent:
    kind: EventKind
    text: str


@dataclass(frozen=True)
class TalkOutcome:
    events: tuple[TalkEvent, ...] = ()
    quit: bool = False


def session_path(data_dir: Path) -> Path:
    return Path(data_dir) / SESSION_FILE


def load_session(data_dir: Path) -> dict[str, str]:
    path = session_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def save_session(data_dir: Path, **fields: str) -> None:
    data = load_session(data_dir)
    data.update({key: value for key, value in fields.items() if value})
    path = session_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_talk(
    process,
    identities,
    data_dir: Path,
    subject_id: str | None,
    speaker: str | None,
    channel: str | None,
    carriers: tuple[CarrierEntry, ...],
) -> TalkSession:
    stored = load_session(data_dir)
    subject_id = (subject_id or stored.get("subject_id") or "").strip()
    speaker = (speaker or stored.get("speaker") or "").strip()
    if not subject_id:
        raise TalkSetupError(
            "错误：未指定主体。首次请：python -m jshi.app.cli talk stone --speaker dp"
        )
    try:
        identities.get(subject_id)
    except KeyError:
        raise TalkSetupError(
            f"错误：主体 {subject_id} 不存在。"
            f"请先：python -m jshi.app.cli create {subject_id}"
        ) from None
    if not speaker:
        raise TalkSetupError(
            "错误：未指定说话人。首次请加 --speaker 名字，之后用 /speaker 更换（长期有效）。"
        )
    save_session(data_dir, subject_id=subject_id, speaker=speaker)
    return TalkSession(
        process,
        data_dir=data_dir,
        subject_id=subject_id,
        speaker=speaker,
        channel=channel,
        carriers=carriers,
    )


class TalkSession:
    def __init__(
        self,
        process,
        *,
        data_dir: Path,
        subject_id: str,
        speaker: str,
        channel: str | None,
        carriers: tuple[CarrierEntry, ...],
    ) -> None:
        self.process = process
        self.data_dir = Path(data_dir)
        self.subject_id = subject_id
        self.speaker = speaker
        self.channel = channel
        self.carriers = carriers
        self.last_line = ""
        self.last_plan = None
        self._turn_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    def handle(self, line: str) -> TalkOutcome:
        line = line.strip()
        if not line:
            return TalkOutcome()
        if line in {"/quit", "/exit", "/q"}:
            if self.busy:
                return TalkOutcome(
                    (TalkEvent("notice", "上一轮尚未结束，请稍候。"),)
                )
            return TalkOutcome(quit=True)
        if line in {"/help", "/?"}:
            return TalkOutcome((TalkEvent("overlay", HELP_TEXT),))
        if line == "/who":
            return TalkOutcome(
                (
                    TalkEvent(
                        "notice",
                        f"对象 {self.speaker}（长期，存在 {session_path(self.data_dir)}）",
                    ),
                )
            )
        if line == "/speaker" or line.startswith("/speaker "):
            return self._handle_speaker(line)
        if line == "/context":
            return TalkOutcome((TalkEvent("overlay", self._context_text()),))
        if line == "/plan":
            return TalkOutcome((TalkEvent("overlay", self._plan_text()),))
        if line == "/prompt":
            return TalkOutcome((TalkEvent("overlay", self._prompt_text()),))
        if line.startswith("/"):
            return TalkOutcome((TalkEvent("notice", "未知命令。输入 /help"),))
        return self._handle_utterance(line)

    def _handle_speaker(self, line: str) -> TalkOutcome:
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return TalkOutcome(
                (
                    TalkEvent(
                        "notice",
                        f"当前对象 {self.speaker}。用法：/speaker 名字",
                    ),
                )
            )
        self.speaker = parts[1].strip()
        save_session(self.data_dir, subject_id=self.subject_id, speaker=self.speaker)
        return TalkOutcome(
            (TalkEvent("notice", f"对象改为 {self.speaker}（已记住）"),)
        )

    def _handle_utterance(self, line: str) -> TalkOutcome:
        if not self._turn_lock.acquire(blocking=False):
            return TalkOutcome(
                (TalkEvent("notice", "上一轮尚未结束，请稍候。"),)
            )
        try:
            try:
                result = self.process.experience(
                    self.subject_id,
                    line,
                    object_ref=self.speaker,
                    channel=self.channel,
                    carriers=self.carriers,
                )
            except ValueError as exc:
                return TalkOutcome((TalkEvent("notice", f"错误：{exc}"),))
            except Exception as exc:
                return TalkOutcome((TalkEvent("notice", f"调用失败：{exc}"),))
            self.last_line = line
            self.last_plan = result.response_plan
            spoken = result.action_text.strip() if result.action_text else ""
            view = self.process.activity_ledger.current_context_view(self.subject_id)
            meta = (
                f"[{result.response_plan.mode}；"
                f"{result.speaker.label}/{result.speaker.status}；"
                f"活跃区 v{view.version} 段{len(view.segment_refs)}]"
            )
            return TalkOutcome(
                (
                    TalkEvent("speech", spoken or "（本轮未开口）"),
                    TalkEvent("meta", meta),
                )
            )
        finally:
            self._turn_lock.release()

    def _context_text(self) -> str:
        query = self.last_line or "（查看上下文）"
        try:
            preview = self.process.preview_state(
                self.subject_id,
                query,
                object_ref=self.speaker,
                channel=self.channel,
                carriers=self.carriers,
            )
        except ValueError as exc:
            return f"错误：{exc}"
        view = preview.context_view
        lines = [
            f"对象 {preview.speaker.label} {preview.speaker.status} {preview.speaker.object_id}",
            f"活跃区 v{view.version} 段{list(view.segment_refs)}",
        ]
        body = view.context_text.strip()
        lines.append(body if body else "（活跃区为空）")
        lines.append("装载：")
        for report in preview.assembled.source_report:
            lines.append(f"  {report.source}: {len(report.loaded_ids)} 条")
        return "\n".join(lines)

    def _plan_text(self) -> str:
        plan = self.last_plan
        if plan is None:
            return "这一轮还没有回应。先说一句再 /plan。"
        lines = [f"mode={plan.mode} reason={plan.reason or '（无）'}"]
        if not plan.items:
            lines.append("（无 items）")
            return "\n".join(lines)
        for item in plan.items:
            lines.append(f"  [{item.channel}] {item.text}")
        return "\n".join(lines)

    def _prompt_text(self) -> str:
        from jshi.models import EchoModel, ModelRequest, ModelSpeaker
        from jshi.skill import CognitionSkill

        query = self.last_line or "（查看提示词）"
        try:
            preview = self.process.preview_state(
                self.subject_id,
                query,
                object_ref=self.speaker,
                channel=self.channel,
                carriers=self.carriers,
            )
        except ValueError as exc:
            return f"错误：{exc}"
        assembled = preview.assembled
        sp = assembled.speaker
        req = ModelRequest(
            purpose="subject_activity",
            input_text=query,
            subject_state=assembled.subject_state,
            speaker=ModelSpeaker(
                object_id=sp.object_id if sp else "",
                label=sp.label if sp else self.speaker,
                aliases=sp.aliases if sp else (),
                status=sp.status if sp else "",
            ),
        )
        extra = CognitionSkill(EchoModel()).system_extra(req)
        return (
            f"{extra}\n---\n"
            f"user（本轮原文）：{query}\n"
            "身份、承诺与分片 JSON 由适配器在 system 后半段追加；完整 HTTP 报文不落库。"
        )
