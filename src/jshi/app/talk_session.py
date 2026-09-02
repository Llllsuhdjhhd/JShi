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
    ("/context", "", "看本轮活跃区与装载（不调模型）"),
    ("/plan", "", "看上一轮 05 的 response_plan 条目"),
    ("/prompt", "", "看即将发给模型的 system 与 user"),
    ("/timing", "轮数", "上一轮各步耗时（可 /timing 5；默认不刷屏）"),
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
        if line == "/plan":
            return TalkOutcome((TalkEvent("overlay", self._plan_text()),))
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
            meta = (
                f"[{result.response_plan.mode}；"
                f"{result.speaker.label}/{result.speaker.status}；"
                f"动作：{embodied or '无动作'}；"
                f"活跃区 v{view.version} 段{len(view.segment_refs)}]"
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
            req = ModelRequest(
                purpose="subject_activity",
                input_text=query,
                subject_state=assembled.subject_state,
                speaker=speaker,
                now=datetime.now().astimezone(),
                governing_rules=self.process._governing_rules(self.subject_id),
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
