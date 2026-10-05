"""Transport facts stay separate from the person's words and identity."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class InputPart:
    kind: str  # text / audio; other modalities can add parts
    text: str = ""
    reference: str = ""  # reference only; never embed raw audio in a prompt
    media_type: str = ""


@dataclass(frozen=True)
class SpeakerEvidence:
    track_id: str
    object_id: str = ""
    label: str = "未命名的说话人"
    status: str = "unknown"
    method: str = "unidentified"
    confidence: float | None = None
    voiceprint_id: str = ""


@dataclass(frozen=True)
class SceneUtterance:
    text: str
    speaker: SpeakerEvidence
    start_ms: int
    end_ms: int
    overlap: bool = False
    input_id: str = ""
    recorded_start_at_ms: float | None = None
    recorded_end_at_ms: float | None = None
    received_at_ms: float | None = None
    identity_note: str = ""


@dataclass(frozen=True)
class InputEnvelope:
    session_id: str
    source: str
    parts: tuple[InputPart, ...]
    speaker: SpeakerEvidence
    input_id: str = field(default_factory=lambda: uuid4().hex)
    start_ms: int = 0
    end_ms: int = 0
    final: bool = True
    overlap: bool = False
    delivery_context: str = ""
    utterances: tuple[SceneUtterance, ...] = ()
    current_utterances: tuple[SceneUtterance, ...] = ()
    deferred_interrupt: bool = False
    jev_calls: tuple = ()
    review_context: str = ""
    review_candidates: tuple = ()

    def __post_init__(self) -> None:
        if not self.session_id or not self.source or not self.speaker.track_id:
            raise ValueError("envelope requires session, source and speaker track")
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("invalid envelope time range")

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.text).strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def context_note(self) -> str:
        notes = ["【输入信封】语音转写；设备来源不等于说话人身份。"]
        if self.speaker.method == "voice_scene":
            notes.append("当前输入是现场观察，没有唯一的已确认对象。不要把现场当成一个来客；"
                         "可等待、更新片场或面向全场说话，不要求每段先报姓名。")
        elif self.speaker.status == "unknown":
            notes.append("当前声音姓名未知，程序可以用匿名对象维持同一声音的连续性。不要声称完全不能辨认声音；可以自然询问怎么称呼，不要求日常聊天先验证身份；不要猜成已有对象，"
                         "不要把设备名当人名。身份明确前不取某个熟人的个人安排。")
        if self.overlap:
            notes.append("存在重叠发言，说话人或内容可能不清楚；必要时请一位先说。")
        if self.delivery_context:
            notes.append(self.delivery_context)
        if self.utterances:
            import json
            if self.current_utterances:
                notes.append("【本批新发言】当前语句原文见【此时的输入】，以下仅列对应的身份与时间。新发言一起处理，"
                             "不要把不同对象的话混成一个人的要求；后面的语音现场只含历史观察。\n"
                             + json.dumps([{k:v for k,v in asdict(u).items() if k != 'text'} for u in self.current_utterances], ensure_ascii=False))
            notes.append("【语音现场】以下是现场观察，不是所有人都在向你提问。对象可以未知；"
                         "结合片场决定 respond/think/ignore/wait，不逐句机械回应或逐句追问身份。"
                         "无明确个人对象时可以向全场说话。response_plan.items 可有多个 verbal，"
                         "每项用 target_ids 指定现场对象 id，空数组表示全场；按数组先后顺序播放。"
                         "不靠旧事问答确认声纹，不宣称能凭内容分辨声音。"
                         "自我介绍的会话关联和 voiceprint_match 是程序事实；不要推翻已给出的关联。\n"
                         "本轮输入仅是本批新收到的发言；历史现场不是再次发来的话。写场记录各对象的发言、"
                         "‘声音归属待定’表示未能归属的音频，不表示一个固定人物；这些语句可能来自不同人，不把它们合并成同一人的记忆。"
                         "对话去向、未完问题与待接续关系；不把多人的话合成一人，不把未完句补成已确定的意图。"
                         "没有需要口头回应的内容仍可写场并等待。匿名声纹对象可延续，不必每轮询问姓名。\n"
                         "程序提供声纹匹配；临时说话人编号不是身份。某句未匹配不表示没有声纹能力，"
                         "也不表示出现了新人；说明本句归属不确定即可。\n"
                         "日常称呼与记忆关联接受明确自我介绍，不要求旧事考问或身份验证。"
                         "名字关联不作为权限或利益操作的授权证据，这类操作单独处理。\n"
                         "简短问候、‘干啥呢’或疲倦等日常表达先按当前意思接话；不要自动重播旧工具报告。"
                         "合成失败表示没有完成播报，不表示用户要求再听一遍；继续播报须结合当前请求。\n"
                         "已有回应已解答的同一问题，不换个措辞再答一遍；本批补充若在上一回应生成前到达，"
                         "先核对现有回应是否已经覆盖，只补未回答的内容。解释延迟仅按程序提供的运行事实，"
                         "没有本轮工具执行或错误证据，不把慢归因于查天气、交通或设备故障。\n"
                         + json.dumps([asdict(u) for u in self.utterances if u not in self.current_utterances], ensure_ascii=False))
        return "\n".join(notes)
