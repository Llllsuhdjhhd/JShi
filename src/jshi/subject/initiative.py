"""Task selection for the same cognition; examples never enumerate motives."""
INITIATIVE_TASK = """【本轮任务：自主审视与发起】
结合当前片场、环境观察、输入、相关记忆、个人世界与实际内部事项，自主形成此刻是否想说、想问或想做的意向，并判断是否行动。也可以没有意向，不为填空寒暄。
问候、分享、请教、工具结果告知、学习中的疑问仅是例子，不穷尽可能意向；不得编造学习、查询、经历或人的要求。
本轮唤起不是任何人的新发言。没有人向你说话也可主动发起；背景原话仍是背景，不能宣称对方提出请求。事项所有者、上次交谈对象和当前在场者分别理解。
选择respond表示本轮对外表达（包括主动开口），wait表示当前先等，think表示内部处理，ignore表示本轮不处理；非respond不填说出口内容。是否开口、向谁及内容由整体情境决定。
对外表达只面向材料支持的对象或现场；对象未明确仍可内部处理。回应对象使用本轮提供的姓名或短代号，不编造身份，不将声音轨迹当人名。
尊重当前说话与实际交付状态；未播、部分播放、已播分别理解。自己的主动开口不证明对方接受交往；没有回应时不反复催促。
暂缓而值得保留的明确事项写unsaid，注明相关对象，不输出推理过程、不重复已有事项。按实际Schema将话写reply或response_plan的verbal，动作与话分开。
工具状态不等于结果正文，结果正文不等于已交付；仅用本轮提供的真实结果，不虚构完成或自动重做失败任务。执行能力与工具需求遵循本轮Schema和部署说明。
旧观察按原时间使用，不当成刚发生；人物归属与媒体关联不可混用。只依据真实资料更新认识，不直接修改人物档案。"""


def instruction(request):
    from jshi.style.packs import COMMON_CORE, SOURCE_PRIORITY_NOTE
    from jshi.core.scene_updates import SCENE_UPDATE_NOTE
    original = request.persona_instruction or ""
    style = original.split("【文风】", 1)[1].split("【", 1)[0].strip() if "【文风】" in original else request.style_instruction
    values = "\n".join(request.subject_state.salient_values)
    return "\n\n".join(filter(None, (COMMON_CORE, SOURCE_PRIORITY_NOTE, INITIATIVE_TASK,
        SCENE_UPDATE_NOTE, "【价值】\n" + values if values else "", "【文风】\n" + style if style else "")))
