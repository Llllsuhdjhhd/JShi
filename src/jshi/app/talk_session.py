"""对话会话：斜杠命令与 experience 调用。不依赖 Textual。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from jshi.personalworld import ValueSource
from jshi.privilege import SuperPermissionStore
from jshi.recognition import CarrierEntry
from jshi.subject import ActivityTiming, PersonalKind

SESSION_FILE = "cli_session.json"

# (命令, 参数, 说明)。全屏壳用同一份列表做箭头选择。
TALK_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/speaker", "名字", "切换对象（写入会话，下次启动仍有效）"),
    ("/who", "", "当前对象"),
    ("/style", "名称", "查看或切换写法（已注册的风格包；提示词里不出现配置名）"),
    ("/context", "", "看片场、木头账本与装载（不调模型）"),
    ("/last", "", "上一轮实际装上的回忆与片场（全文）"),
    ("/memory", "", "最近一次记忆落库（全文）"),
    ("/memory_raw", "", "尚未交 09 的账本原文（记忆游标之后）"),
    ("/plan", "", "看上一轮 05 的 response_plan 条目"),
    ("/response", "", "看上一轮模型回复（易读）"),
    ("/response_raw", "", "看上一轮模型原文（解析前）"),
    ("/prompt", "", "看即将发给模型的 system 与 user"),
    ("/timing", "轮数", "上一轮各步耗时（可 /timing 5；默认不刷屏）"),
    ("/tool", "list|raw|id", "看工具过程：05 指示、200 交接、引擎反馈、记挂与包装"),
    ("/login", "密码", "超级权限登录"),
    ("/logout", "", "退出超级权限"),
    ("/rule", "内容", "写入提示词【附加规则】"),
    ("/value", "内容", "写入个人世界：价值观"),
    ("/boundary", "内容", "写入个人世界：边界"),
    ("/commitment", "内容", "写入个人世界：承诺"),
    ("/quit", "", "结束"),
)

TIMING_KEEP = 20


def format_help_text() -> str:
    lines = []
    for name, argument, summary in TALK_COMMANDS:
        usage = f"{name} {argument}".strip() if argument else name
        lines.append(f"{usage:<16} {summary}")
    lines.append(
        "超级权限：先 /login 密码，再 /rule、/value、/boundary、/commitment。"
    )
    lines.append(
        "落点规则：/value、/boundary、/commitment 进个人世界（也会进提示词对应区块）；"
        "/rule 只进提示词【附加规则】，不改长期个人世界。"
    )
    lines.append(
        "/prompt/区块名 看单块：system、user、schema、当前时间、关于你、关于输入、"
        "输入格式示例、价值、回应方式、其余工作、输出格式、承诺、边界、附加规则、"
        "说话人、活跃区、回忆、本轮"
    )
    return "\n".join(lines)


def _memory_source_report(assembled) -> object | None:
    reports = getattr(assembled, "source_report", ()) or ()
    return next((item for item in reports if item.source == "memory"), None)


def _memory_fragments(assembled) -> tuple:
    return tuple(
        item
        for item in (getattr(assembled, "fragments", ()) or ())
        if getattr(item, "source", None) == "memory"
    )


def recall_status(assembled) -> tuple[int, str]:
    """本轮组装里的回忆条数，以及空时的原因（给底栏与 /last）。"""
    fragments = _memory_fragments(assembled)
    report = _memory_source_report(assembled)
    count = len(fragments)
    error = (getattr(report, "error", None) or "").strip() if report else ""
    skipped = len(getattr(report, "skipped_ids", ()) or ()) if report else 0
    if error:
        short = error if len(error) <= 48 else error[:47] + "…"
        return count, f"错误：{short}"
    if count == 0 and skipped:
        return 0, f"预算跳过 {skipped} 条"
    if count == 0:
        return 0, "召回空"
    return count, ""


def _zone_block_count(process, subject_id: str) -> int:
    store = getattr(process, "zone_store", None)
    if store is None:
        return 0
    value = (store.value_narration(subject_id) or "").strip()
    return (1 if value else 0) + len(store.get(subject_id) or ())


def _delivery_label(result) -> str:
    if result is None:
        return "无"
    status = (getattr(result, "status", None) or "").strip() or "未知"
    if status == "skipped":
        reason = getattr(getattr(result, "decision", None), "reason", "") or ""
        labels = {
            "insufficient_memory_data": "数据不足",
            "previous_memory_process_not_finished": "上轮未完",
            "max_retry_reached": "已达重试上限",
        }
        if reason in labels:
            return f"skipped（{labels[reason]}）"
        return f"skipped（{reason}）" if reason else "skipped"
    return status


def format_turn_meta(
    *,
    mode: str,
    speaker_label: str,
    speaker_status: str,
    embodied: str,
    recall_count: int,
    recall_reason: str,
    zone_blocks: int,
    ledger_version: int,
    boot: str,
    delivery: str,
) -> str:
    recall = f"回忆 {recall_count}"
    if recall_reason and recall_count == 0:
        recall += f"（{recall_reason}）"
    action = embodied or "无动作"
    return (
        f"[{mode}；{speaker_label}/{speaker_status}；动作：{action}；"
        f"{recall}；片场 {zone_blocks} 块；木头 v{ledger_version}；"
        f"boot {boot}；投递 {delivery}]"
    )


HELP_TEXT = format_help_text()


def format_activity_timing(timing: ActivityTiming, *, heading: str = "上一轮") -> str:
    lines = [f"{heading} 合计 {timing.total_ms:g}ms"]
    for name, milliseconds in timing.steps:
        lines.append(f"  {name} {milliseconds:g}ms")
    return "\n".join(lines)

_PROMPT_SECTION_ALIASES = {
    "时间": "当前时间",
    "格式示例": "输入格式示例",
    "规则": "附加规则",
    "附加": "附加规则",
}


def _extract_prompt_section(text: str, name: str) -> str | None:
    marker = f"【{name}】"
    index = text.find(marker)
    if index < 0:
        return None
    lines = text[index:].splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if kept and stripped.startswith("【") and stripped.endswith("】") and "：" not in stripped:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _extract_json_schema(text: str) -> str | None:
    lines = [
        line
        for line in text.splitlines()
        if line.startswith("JSON Schema：") or line.startswith("请严格按下面的 JSON Schema")
    ]
    return "\n".join(lines) if lines else None


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
    super_permissions: SuperPermissionStore | None = None,
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
        super_permissions=super_permissions,
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
        super_permissions: SuperPermissionStore | None = None,
        on_reply: Callable[[str], None] | None = None,
    ) -> None:
        self.process = process
        self.data_dir = Path(data_dir)
        self.subject_id = subject_id
        self.speaker = speaker
        self.channel = channel
        self.carriers = carriers
        self.super_permissions = super_permissions
        self.on_reply = on_reply
        self.privileged_object_id: str | None = None
        self.last_line = ""
        self.last_plan = None
        self.last_assembled = None
        self._timings: list[ActivityTiming] = []
        self._turn_lock = threading.Lock()
        self._reply_streamed = False

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
        if line == "/style" or line.startswith("/style "):
            return self._handle_style(line)
        if line == "/speaker" or line.startswith("/speaker "):
            return self._handle_speaker(line)
        if line == "/login" or line.startswith("/login "):
            return self._handle_login(line)
        if line == "/logout":
            return self._handle_logout()
        if line == "/rule" or line.startswith("/rule "):
            return self._handle_rule(line)
        if line == "/value" or line.startswith("/value "):
            return self._handle_value(line)
        if line == "/boundary" or line.startswith("/boundary "):
            return self._handle_boundary(line)
        if line == "/commitment" or line.startswith("/commitment "):
            return self._handle_commitment(line)
        if line == "/context":
            return TalkOutcome((TalkEvent("overlay", self._context_text()),))
        if line == "/last":
            return TalkOutcome((TalkEvent("overlay", self._last_text()),))
        if line == "/memory":
            return TalkOutcome((TalkEvent("overlay", self._memory_text()),))
        if line == "/memory_raw":
            return TalkOutcome((TalkEvent("overlay", self._memory_raw_text()),))
        if line == "/plan":
            return TalkOutcome((TalkEvent("overlay", self._plan_text()),))
        if line == "/response" or line == "/reply":
            return TalkOutcome((TalkEvent("overlay", self._model_response_text()),))
        if line == "/response_raw":
            return TalkOutcome((TalkEvent("overlay", self._model_raw_text()),))
        if line == "/prompt":
            return TalkOutcome((TalkEvent("overlay", self._prompt_text()),))
        if line.startswith("/prompt/"):
            name = line[len("/prompt/"):].strip()
            if not name:
                return TalkOutcome(
                    (TalkEvent("notice", "用法：/prompt/区块名"),)
                )
            return TalkOutcome((TalkEvent("overlay", self._prompt_section(name)),))
        if line == "/timing" or line.startswith("/timing "):
            return self._handle_timing(line)
        if line == "/tool" or line.startswith("/tool "):
            return TalkOutcome((TalkEvent("overlay", self._tool_text(line)),))
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

    def _handle_style(self, line: str) -> TalkOutcome:
        packs = getattr(self.process, "style_packs", None)
        if packs is None:
            return TalkOutcome((TalkEvent("notice", "当前运行没有风格包存储"),))
        registry = getattr(packs, "registry", None)
        labels = []
        display = {}
        if registry is not None:
            for pack in registry.all_packs():
                label = pack.display_name or pack.pack_id
                labels.append(label)
                display[pack.pack_id] = label
        names = "、".join(labels)
        parts = line.split(None, 1)
        current = packs.get(self.subject_id)
        if len(parts) < 2 or not parts[1].strip():
            return TalkOutcome(
                (
                    TalkEvent(
                        "notice",
                        f"当前写法 {display.get(current, current)}。用法：/style {names}",
                    ),
                )
            )
        chosen = packs.set(self.subject_id, parts[1].strip())
        note = f"写法改为 {display.get(chosen, chosen)}（已记住；提示词里仍只称匠石）"
        if chosen != current:
            note += "。注意：有素材才会写小说片场——切换后若素材不足，会先按木头继续攒素材，够了再写场景。"
        return TalkOutcome((TalkEvent("notice", note),))

    def _current_object_id(self) -> str | None:
        profile = self.process.profiles.get(self.speaker)
        if profile is not None:
            return profile.object_id
        matches = self.process.profiles.find_by_names(self.speaker)
        if len(matches) == 1:
            return matches[0].object_id
        return None

    def _require_privileged(self) -> str | None:
        if self.privileged_object_id is None:
            return "需要超级权限：先 /login 密码"
        return None

    def _handle_login(self, line: str) -> TalkOutcome:
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return TalkOutcome((TalkEvent("notice", "用法：/login 密码"),))
        if self.super_permissions is None:
            return TalkOutcome((TalkEvent("notice", "未配置超级权限存储。"),))
        object_id = self._current_object_id()
        if object_id is None:
            return TalkOutcome(
                (TalkEvent("notice", "当前对象未匹配到唯一档案，无法登录。"),)
            )
        if not self.super_permissions.verify(object_id, parts[1].strip()):
            return TalkOutcome(
                (TalkEvent("notice", "密码错误或该对象无超级权限。"),)
            )
        self.privileged_object_id = object_id
        return TalkOutcome((TalkEvent("notice", "已登录（超级权限）。"),))

    def _handle_logout(self) -> TalkOutcome:
        self.privileged_object_id = None
        return TalkOutcome((TalkEvent("notice", "已退出超级权限。"),))

    @staticmethod
    def _parse_rule(body: str) -> tuple[str, str] | None:
        body = (body or "").strip()
        if not body:
            return None
        known = {
            "关于你",
            "关于输入",
            "输入格式示例",
            "价值",
            "回应方式",
            "其余工作",
            "输出格式",
        }
        parts = body.split(None, 1)
        if len(parts) == 2 and parts[0] in known:
            return parts[0], parts[1].strip()
        return "general", body

    def _handle_rule(self, line: str) -> TalkOutcome:
        denied = self._require_privileged()
        if denied:
            return TalkOutcome((TalkEvent("notice", denied),))
        parts = line.split(None, 1)
        body = parts[1] if len(parts) > 1 else ""
        parsed = self._parse_rule(body)
        if parsed is None:
            return TalkOutcome(
                (TalkEvent("notice", "用法：/rule 内容（或 /rule 区块名 内容）"),)
            )
        section, content = parsed
        prompt_rules = getattr(self.process, "prompt_rules", None)
        if prompt_rules is None:
            return TalkOutcome((TalkEvent("notice", "未配置提示词规则存储。"),))
        rule = prompt_rules.add(
            self.subject_id,
            content,
            section=section,
            source=f"{self.speaker}:{self.privileged_object_id}",
        )
        return TalkOutcome((TalkEvent("notice", f"已写入提示词规则：{rule.id}"),))

    def _handle_value(self, line: str) -> TalkOutcome:
        return self._handle_personal_write(line, "/value", "value")

    def _handle_boundary(self, line: str) -> TalkOutcome:
        return self._handle_personal_write(line, "/boundary", "boundary")

    def _handle_commitment(self, line: str) -> TalkOutcome:
        denied = self._require_privileged()
        if denied:
            return TalkOutcome((TalkEvent("notice", denied),))
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return TalkOutcome((TalkEvent("notice", "用法：/commitment 内容"),))
        item = self.process.add_personal_item(
            self.subject_id,
            PersonalKind.COMMITMENT,
            parts[1].strip(),
        )
        return TalkOutcome((TalkEvent("notice", f"已写入承诺：{item.id}"),))

    def _handle_personal_write(
        self,
        line: str,
        command: str,
        role: str,
    ) -> TalkOutcome:
        denied = self._require_privileged()
        if denied:
            return TalkOutcome((TalkEvent("notice", denied),))
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return TalkOutcome((TalkEvent("notice", f"用法：{command} 内容"),))
        values = getattr(self.process, "values", None)
        if values is None:
            return TalkOutcome((TalkEvent("notice", "当前个人世界没有 100 接口。"),))
        try:
            candidate = values.propose_value(
                self.subject_id,
                parts[1].strip(),
                role=role,
                source_type=ValueSource.HUMAN_INTERVENTION,
                source_id=self.privileged_object_id,
            )
            accepted = values.review_value(
                candidate.id,
                "accept",
                "super user command",
                self.speaker,
            )
        except (KeyError, ValueError) as exc:
            return TalkOutcome((TalkEvent("notice", f"错误：{exc}"),))
        return TalkOutcome((TalkEvent("notice", f"已写入{role}：{accepted.id}"),))

    def _handle_utterance(self, line: str) -> TalkOutcome:
        if not self._turn_lock.acquire(blocking=False):
            return TalkOutcome(
                (TalkEvent("notice", "上一轮尚未结束，请稍候。"),)
            )
        try:
            try:
                def _early(text: str) -> None:
                    if self.on_reply is not None:
                        self._reply_streamed = True
                        self.on_reply(text)

                result = self.process.experience(
                    self.subject_id,
                    line,
                    object_ref=self.speaker,
                    channel=self.channel,
                    carriers=self.carriers,
                    on_reply=_early,
                )
            except ValueError as exc:
                return TalkOutcome((TalkEvent("notice", f"错误：{exc}"),))
            except Exception as exc:
                return TalkOutcome((TalkEvent("notice", f"调用失败：{exc}"),))
            self.last_line = line
            self.last_plan = result.response_plan
            self.last_assembled = result.current_state
            timing = result.timing or getattr(
                self.process, "last_activity_timing", None
            )
            if isinstance(timing, ActivityTiming):
                self._timings.append(timing)
                if len(self._timings) > TIMING_KEEP:
                    del self._timings[:-TIMING_KEEP]
            spoken = result.action_text.strip() if result.action_text else ""
            if self._reply_streamed:
                # 已由 on_reply 流式打印,本轮的 speech 事件不再重复(置空,前端跳过)。
                speech_text = ""
                self._reply_streamed = False
            else:
                speech_text = spoken or "（本轮未开口）"
            embodied = result.response_plan.embodied_text().strip()
            view = self.process.activity_ledger.current_context_view(self.subject_id)
            recall_n, recall_reason = recall_status(result.current_state)
            meta = format_turn_meta(
                mode=result.response_plan.mode,
                speaker_label=result.speaker.label,
                speaker_status=result.speaker.status,
                embodied=embodied,
                recall_count=recall_n,
                recall_reason=recall_reason,
                zone_blocks=_zone_block_count(self.process, self.subject_id),
                ledger_version=view.version,
                boot=getattr(self.process, "last_boot", "否") or "否",
                delivery=_delivery_label(
                    getattr(self.process, "last_memory_control", None)
                ),
            )
            return TalkOutcome(
                (
                    TalkEvent("speech", speech_text),
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
        except Exception as exc:
            return f"预览失败：{exc}"
        from jshi.style import DISPLAY_NAMES

        view = preview.context_view
        pack_id = view.style_pack_id
        if not pack_id and hasattr(self.process, "style_packs"):
            pack_id = self.process.style_packs.get(self.subject_id)
        pack_label = DISPLAY_NAMES.get(pack_id, pack_id or "")
        zone_n = _zone_block_count(self.process, self.subject_id)
        lines = [
            f"对象 {preview.speaker.label} {preview.speaker.status} {preview.speaker.object_id}",
            f"写法 {pack_label or '木头'}；片场 {zone_n} 块；木头账本 v{view.version} {len(view.context_text or '')}字",
        ]
        render = getattr(getattr(self.process, "zone_store", None), "render", None)
        zone_text = render(self.subject_id) if callable(render) else ""
        lines.append("片场：")
        lines.append(zone_text.strip() if zone_text and zone_text.strip() else "（空）")
        lines.append("木头账本：")
        body = (view.context_text or "").strip()
        lines.append(body if body else "（空）")
        lines.append("装载：")
        for report in preview.assembled.source_report:
            if report.source == "memory":
                count, reason = recall_status(preview.assembled)
                suffix = f"（{reason}）" if reason else ""
                skipped = (
                    f"，跳过 {len(report.skipped_ids)}"
                    if report.skipped_ids and count
                    else ""
                )
                lines.append(f"  memory: {count} 条{suffix}{skipped}")
                continue
            skipped = f"，跳过 {len(report.skipped_ids)}" if report.skipped_ids else ""
            err = f"，错误：{report.error}" if report.error else ""
            lines.append(
                f"  {report.source}: {len(report.loaded_ids)} 条{skipped}{err}"
            )
        return "\n".join(lines)

    def _last_text(self) -> str:
        assembled = self.last_assembled
        if assembled is None:
            return "这一轮还没有。先说一句再 /last。"
        n, reason = recall_status(assembled)
        lines = [f"本轮输入：{assembled.input_text}"]
        header = f"回忆 {n} 条"
        if reason:
            header += f"（{reason}）"
        lines.append(header)
        fragments = _memory_fragments(assembled)
        if not fragments:
            lines.append("（无）")
        else:
            for item in fragments:
                label = getattr(item, "id", "") or ""
                content = (getattr(item, "content", "") or "").strip() or "（空正文）"
                lines.append(f"- {label}")
                lines.append(content)
        report = _memory_source_report(assembled)
        if report is not None and report.skipped_ids:
            lines.append("跳过：" + "、".join(report.skipped_ids))
        if report is not None and report.error:
            lines.append(f"组装错误：{report.error}")
        boot = getattr(self.process, "last_boot", "否") or "否"
        lines.append(f"boot {boot}")
        render = getattr(getattr(self.process, "zone_store", None), "render", None)
        zone_text = render(self.subject_id) if callable(render) else ""
        lines.append("本轮后片场：")
        lines.append(zone_text.strip() if zone_text and zone_text.strip() else "（空）")
        return "\n".join(lines)

    def _memory_text(self) -> str:
        ledger = getattr(self.process, "activity_ledger", None)
        if ledger is None or not hasattr(ledger, "list_ingest_entries"):
            return "当前运行没有经历账本。"
        from jshi.experienceledger import ConsumerKind

        cursor = ledger.consumer_cursor(self.subject_id, ConsumerKind.MEMORY)
        head = ledger.head_sequence(self.subject_id)
        lines = [f"记忆游标 {cursor} / 账本末段 {head}"]
        this_turn = getattr(self.process, "last_memory_control", None)
        if this_turn is not None:
            decision = getattr(this_turn, "decision", None)
            reason = getattr(decision, "reason", "") or ""
            lines.append(
                f"本轮投递 {this_turn.status}"
                + (f"（{reason}）" if reason else "")
            )
            if this_turn.error:
                lines.append(f"错误：{this_turn.error}")
        entries = list(ledger.list_ingest_entries(self.subject_id) or ())
        if not entries:
            lines.append("尚无落库记录。")
            return "\n".join(lines)
        last = entries[-1]
        when = last.ingested_at.isoformat() if last.ingested_at else "未完成"
        lines.append(
            f"最近一批 {last.status} id={last.ingest_id[:8]} 于 {when}"
        )
        if last.reason:
            lines.append(f"原因：{last.reason}")
        event_ids = tuple(last.memory_event_ids or ())
        if not event_ids:
            lines.append("本批无封存事件 id。")
            return "\n".join(lines)
        lines.append(f"事件 {len(event_ids)} 条：")
        for event_id, text in self._ingest_event_texts(event_ids):
            lines.append(f"- {event_id}")
            lines.append(text)
        return "\n".join(lines)

    def _ingest_event_texts(self, event_ids: tuple[str, ...]) -> list[tuple[str, str]]:
        backend = getattr(getattr(self.process, "memory", None), "_backend", None)
        pipeline = getattr(backend, "_pipeline", None)
        repo = getattr(pipeline, "event_repo", None)
        rows: list[tuple[str, str]] = []
        for event_id in event_ids:
            if repo is None:
                rows.append((event_id, "（当前后端不能按 id 取正文）"))
                continue
            try:
                event = repo.get(event_id)
            except Exception as exc:  # noqa: BLE001
                rows.append((event_id, f"（读取失败：{exc}）"))
                continue
            if event is None:
                rows.append((event_id, "（库中无此条）"))
                continue
            summaries = getattr(event, "summaries", None) or {}
            text = (
                summaries.get("L1")
                or getattr(event, "content_raw", None)
                or ""
            ).strip() or "（无摘要）"
            rows.append((event_id, text))
        return rows

    def _memory_raw_text(self) -> str:
        ledger = getattr(self.process, "activity_ledger", None)
        if ledger is None:
            return "当前运行没有经历账本。"
        from jshi.experienceledger import ConsumerKind

        cursor = ledger.consumer_cursor(self.subject_id, ConsumerKind.MEMORY)
        head = ledger.head_sequence(self.subject_id)
        pending = list(ledger.list_experiences(self.subject_id, after_sequence=cursor))
        lines = [
            f"记忆游标 {cursor} / 账本末段 {head}",
            f"未交 09：{len(pending)} 段",
        ]
        if not pending:
            lines.append("没有尚未落库的账本段。")
            return "\n".join(lines)
        for segment in pending:
            kind = getattr(segment.output_kind, "value", segment.output_kind)
            speaker = self._segment_label(segment)
            body = (segment.text_raw or "").strip() or "（空正文）"
            lines.append(f"- seq {segment.sequence} {kind} {speaker}")
            lines.append(body)
        return "\n".join(lines)

    def _segment_label(self, segment) -> str:
        actor = getattr(segment, "actor_object_id", None)
        objects = dict(getattr(segment, "objects", None) or {})
        if actor:
            for name, oid in objects.items():
                if oid == actor:
                    return name
            profiles = getattr(self.process, "profiles", None)
            if profiles is not None:
                profile = profiles.get(actor)
                if profile is not None and getattr(profile, "label", None):
                    return profile.label
            return actor
        if objects:
            return "、".join(objects)
        kind = getattr(segment, "actor_kind", None)
        value = getattr(kind, "value", kind)
        if value == "subject":
            return "匠石"
        return "（无对象）"

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

    def _model_response_text(self) -> str:
        resp = getattr(self.process, "last_model_response", None)
        if resp is None:
            return "这一轮还没有回应。先说一句再 /response。"
        plan = resp.response_plan
        # 写场结果（两调用时独立于回复；未注入时与回复相同）。
        wresp = getattr(self.process, "last_write_response", None) or resp
        lines = [f"mode: {plan.mode}", f"reason: {plan.reason or '（无）'}"]
        verbal = plan.verbal_text().strip()
        embodied = plan.embodied_text().strip()
        lines.append(f"语言: {verbal or '（无）'}")
        lines.append(f"动作: {embodied or '（无动作）'}")
        edits = getattr(wresp, "zone_edit", ()) or ()
        if edits:
            lines.append("片场 edit:")
            for op in edits:
                name = op.get("op")
                bid = op.get("id")
                if name == "del":
                    lines.append(f"  - del {bid}")
                elif name == "mod":
                    lines.append(f"  - mod {bid} → {op.get('text', '')}")
                else:
                    lines.append(f"  - {name} {bid}")
        scene = getattr(resp, "scene", ()) or ()
        if scene:
            lines.append(f"写场景（{len(scene)} 块）:")
            for i, block in enumerate(scene, 1):
                lines.append(f"  B{i}  {block}")
        rc = getattr(wresp, "rewritten_context", "") or ""
        if rc:
            lines.append(f"现场: {rc[:200]}{'…' if len(rc) > 200 else ''}")
        # 本轮更新后的片场（对话记录，含回话）
        render = getattr(getattr(self.process, "zone_store", None), "render", None)
        if callable(render):
            zone_text = render(self.subject_id)
            if zone_text:
                lines.append("本轮后片场：")
                lines.extend(zone_text.splitlines())
        return "\n".join(lines)

    def _model_raw_text(self) -> str:
        resp = getattr(self.process, "last_model_response", None)
        if resp is None:
            return "这一轮还没有回应。先说一句再 /response_raw。"
        raw = (getattr(resp, "raw_text", "") or "").strip()
        if not raw:
            return "这一轮没有保存模型原文。"
        return raw

    def _tool_text(self, line: str) -> str:
        from jshi.tool.view import format_tool_process

        service = getattr(self.process, "tool_service", None)
        if service is None:
            return "当前过程没有工具模块。"
        parts = line.split()
        listing = False
        raw = False
        item_id = ""
        for token in parts[1:]:
            if token == "list":
                listing = True
            elif token == "raw":
                raw = True
            elif token == "last":
                continue
            elif token == "help":
                return (
                    "用法：/tool           当前对象最近一次过程\n"
                    "      /tool list      交接与记挂一览\n"
                    "      /tool raw       引擎反馈少截断\n"
                    "      /tool <id>      按 intake_id 或 task_id 看一本"
                )
            else:
                item_id = token
        object_id = self._current_object_id() or ""
        if not object_id:
            return "当前对象未匹配到唯一档案，无法按对象查看。"
        resp = getattr(self.process, "last_model_response", None)
        intent = getattr(resp, "tool_intent", None) if resp is not None else None
        last_use_tool = None if resp is None else intent is not None
        last_need = getattr(intent, "need", "") or ""
        last_verbal = ""
        plan = self.last_plan
        if plan is not None:
            last_verbal = plan.verbal_text().strip()
        return format_tool_process(
            service,
            subject_id=self.subject_id,
            object_id=object_id,
            last_use_tool=last_use_tool,
            last_need=last_need,
            last_verbal=last_verbal,
            item_id=item_id,
            listing=listing,
            raw=raw,
        )

    def _prompt_parts(self) -> tuple[str, str] | str:
        from dataclasses import replace

        from jshi.models import EchoModel, ModelRequest, ModelSpeaker, build_system, build_user
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
            assembled = preview.assembled
            sp = assembled.speaker
            speaker = None
            if sp is not None:
                speaker = ModelSpeaker(
                    object_id=sp.object_id,
                    label=sp.label or self.speaker,
                    aliases=sp.aliases,
                    status=sp.status,
                    reason=sp.reason,
                )
            style_instruction, style_first = self.process._style_fields(
                self.subject_id, assembled.context_view
            )
            persona_instruction, persona_schema, boot, _zone = (
                self.process._persona_fields(self.subject_id, assembled)
            )
            persona_user_text = (
                self.process._persona_user_text(assembled, boot=boot)
                if persona_instruction
                else ""
            )
            req = ModelRequest(
                purpose="subject_activity",
                input_text=query,
                subject_state=assembled.subject_state,
                speaker=speaker,
                now=datetime.now().astimezone(),
                governing_rules=self.process._governing_rules(self.subject_id),
                style_instruction=style_instruction,
                style_first=style_first,
                persona_instruction=persona_instruction,
                persona_schema=persona_schema,
                boot=boot,
                persona_user_text=persona_user_text,
                context=self.process._model_context(
                    self.subject_id,
                    assembled.recalled,
                    assembled.fragments,
                    assembled.context_view,
                ),
            )
            req = replace(req, system_extra=CognitionSkill(EchoModel()).system_extra(req))
            return build_system(req), build_user(req)
        except ValueError as exc:
            return f"错误：{exc}"
        except Exception as exc:
            return f"组装提示词失败：{exc}"

    def _prompt_text(self) -> str:
        result = self._prompt_parts()
        if isinstance(result, str):
            return result
        system_text, user_text = result
        return f"system\n{system_text}\n---\nuser\n{user_text}"

    def _prompt_section(self, name: str) -> str:
        result = self._prompt_parts()
        if isinstance(result, str):
            return result
        system_text, user_text = result

        name = name.strip()
        if name in {"system", "sys"}:
            return system_text
        if name == "user":
            return user_text
        if name in {"schema", "json-schema", "jsonschema"}:
            schema = _extract_json_schema(system_text)
            return schema or "没有找到 JSON Schema。"
        resolved = _PROMPT_SECTION_ALIASES.get(name, name)
        for text in (system_text, user_text):
            section = _extract_prompt_section(text, resolved)
            if section:
                return section
        return f"没有找到提示词区块：{name}"

    def _handle_timing(self, line: str) -> TalkOutcome:
        parts = line.split()
        count = 1
        if len(parts) > 2:
            return TalkOutcome(
                (TalkEvent("notice", "用法：/timing 或 /timing 5"),)
            )
        if len(parts) == 2:
            try:
                count = int(parts[1])
            except ValueError:
                return TalkOutcome(
                    (TalkEvent("notice", "用法：/timing 或 /timing 5"),)
                )
            if count < 1:
                return TalkOutcome(
                    (TalkEvent("notice", "用法：/timing 或 /timing 5"),)
                )
            count = min(count, TIMING_KEEP)
        if not self._timings:
            return TalkOutcome(
                (TalkEvent("notice", "还没有走完一轮。先说一句再 /timing。"),)
            )
        chosen = self._timings[-count:]
        start = len(self._timings) - len(chosen) + 1
        blocks = []
        for index, timing in enumerate(chosen, start=start):
            heading = "上一轮" if len(chosen) == 1 else f"第{index}轮"
            blocks.append(format_activity_timing(timing, heading=heading))
        return TalkOutcome((TalkEvent("overlay", "\n".join(blocks)),))
