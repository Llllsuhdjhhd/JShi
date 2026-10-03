"""JEV decides whether an utterance warrants interrupting ongoing playback."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object


@dataclass(frozen=True)
class InterruptDecision:
    action: str = "respond"  # ignore / resume / stop / respond / clarify
    reason: str = "new utterance"
    claimed_name: str = ""


def explicit_name(text: str) -> str:
    match = re.match(r"\s*(?:我叫|我是)\s*([^，。！？!?\n]{1,30})(?:[，。！？!?\n]|$)", text)
    if not match:
        return ""
    name = re.split(r"你好|您好|大家好|我想|我在", match.group(1), maxsplit=1)[0].strip()
    if re.fullmatch(r"[A-Za-z](?:[A-Za-z\s]*[A-Za-z])?", name):
        name = re.sub(r"\s+", "", name)
    return name


class VoiceJEV:
    def __init__(self, model=None) -> None:
        self.model = model

    def decide(self, subject_id: str, text: str, speaker: dict, delivery: dict, *, overlap: bool = False) -> InterruptDecision:
        normalized = text.strip().rstrip("。！!，,？?").strip()
        if normalized in {"停", "停止", "别说了", "停一下", "等等", "等一下"}:
            return InterruptDecision("stop", "explicit stop")
        active = bool(delivery) and delivery.get("state") not in {"completed", "stopped", "failed"}
        if overlap:
            return InterruptDecision("resume" if active else "ignore", "overlapping speech; avoid blind interruption")
        if not normalized:
            return InterruptDecision("resume", "no utterance")
        if normalized in {"了", "就", "那个", "的", "是"}:
            return InterruptDecision("resume", "incomplete fragment; wait for meaningful utterance")
        if normalized in {"嗯", "啊", "哦", "是", "对", "对的", "好", "好的", "那个", "就", "了", "哈哈", "哈哈哈哈"}:
            return InterruptDecision("resume", "backchannel or incomplete fragment; keep playing")
        claimed = explicit_name(text)
        if self.model is None:
            return InterruptDecision("resume" if active else "respond", "JEV rule fallback", claimed)
        request = ModelRequest(
            purpose="voice_jev",
            input_text=json.dumps({"utterance": text, "speaker": speaker, "delivery": delivery}, ensure_ascii=False),
            subject_state=SubjectState(subject_id, "匠石", "判断当前插话如何接续"),
            system_extra=("你是匠石的 JEV，判断这一批现场发言是否在对匠石说话、是否需要回应。按 delivery.state 判断状态，不能假定一直在播放。只输出 JSON："
                '{"action":"ignore|resume|stop|respond|clarify","reason":"简短依据","claimed_name":""}。'
                "空闲或刚说完时，旁人聚会聊天、对别人提问、笑声、附和和没有回应必要的结束语选 ignore：只留现场，不触发主流程回答。"
                "正在播放时判断是否明确插话；正在对话时结合现场判断是否承接对匠石的问题，不因出现问号就认定在问匠石。"
                "resume 表示继续播放，用于附和、噪声、不完整碎片、旁人互聊或并非对匠石说的话；"
                "stop 用于明确要求停止；respond 仅用于明确向匠石提出的新问题、补充、纠正或接话；"
                "clarify 仅用于明确向匠石说话但需要澄清的内容。换人本身不是打断依据。不要生成回答。"
                "claimed_name 只取说话人明确的自我介绍，不取提及的其他人。"
                "身份未明或换了对象时，不要把附和擅自归给原说话人。"
                "姓名未知、匿名访客、声音归属待定都不是忽略或拒绝进入主流程的理由。"
                "新来者向匠石打招呼、自我介绍、叫匠石名字纠正其名字、询问能否听见，应按内容选 respond 或 clarify。"
                "没有姓名也能正常对话。判断对象是匠石的依据是称呼、语义和上下文，不能要求先登记声纹或确认姓名。"
                "delivery.attention 是接话关注权重：近期对话者更值得关注，但不是身份或声纹证据，"
                "不能仅凭高权重打断，也不能忽略新来者的明确提问。结合 delivery.scene 中各人的发言判断。"
                "人声比普通背景声更值得分析；响亮、尖锐、突出不等于向匠石说话，不据此停止播放。"
                "转写可能包含扬声器回声；如果内容只是复述当前回应，且没有明显向匠石接话，继续播放。"),
        )
        try:
            data = parse_json_object(self.model.generate(request).text)
            action = data.get("action")
            if action not in {"ignore", "resume", "stop", "respond", "clarify"}:
                raise ValueError("invalid JEV action")
            # Bind only explicit self-introductions verified by the program.
            return InterruptDecision(action, str(data.get("reason", ""))[:200], claimed)
        except Exception:
            return InterruptDecision("resume", "JEV unavailable; keep playing", claimed)
