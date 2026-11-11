"""Shared scene reasoning, selected from existing evidence without another call."""

SCENE_GROUNDING_RULE = '''【先理解现场，再判断交往】
先还原人物、当前活动、谁接谁的话及匠石的角色，再解释本句指代。已确认关系可沿用，惯常名单和未入镜不证明身份或离场。先看最新原话与实际接续，再用旧片场解释；片场里的“喊名才接”等历史判断不是永久规则。
比较人际问答与向匠石交谈两种解释，依据称呼的实际指向、内容承接、行为能力和有效视听对应。入口声称有称呼、承接或面对面时，核对对应原话或观察；找不到就保留缺口，不以入口结论证明自身正确。自己的插话、过渡语及其摘要也不证明对方参与。
话中经历或行为不符合匠石且缺少对匠石交往的实际依据时，保留人际解释，不默认“在问我但我做不到”而插话解释能力。能力不符不是禁答条件：真实交谈中的玩笑、假设、能力询问照常接住。新呼唤、自然面对设备的招呼或已成立交谈的跟进也可回应；身份未知不妨碍接话，跟进不必再次点名。
现场可并行活动，一次呼唤不改变所有人的受话关系。认识更新仅修正所述人物与事件范围，不能覆盖后来的新呼唤或转向。保留已说／已播事实与未解缺口，不把解释改写成原话。'''


def scene_cautions(lines, environment=None):
    """Select only evidenced conditions; this does not classify a scene or person."""
    notes = []
    if any(row.get('overlap') or row.get('person_scope') == 'unaligned_cloud_span' for row in lines):
        notes.append('本批存在叠音或未可靠分人的原声：逐条保留对象与语义缺口，不跨人补成请求。')
    if any(row.get('asr_previous') for row in lines):
        notes.append('本批含原音频转写修订：对照前后版本与已交付事实，后到不自动更准确，不当作重复提问。')
    if any(row.get('audio_delivery') for row in lines):
        notes.append('audio_delivery为对应音频末尾实际收包到识别到达的间隔（秒），不是音频长度、当前等待或过时证明。结合原话、是否已答及后续转向判断；迟到但未处理的明确呼唤或请求仍可回应。')
    if isinstance(environment, dict) and environment:
        if environment.get('status') in {'description_stale', 'description_time_unknown'}:
            notes.append('视觉描述已过期或依据时间未知：仅作历史材料，不能据此确认当前朝向、在场或受话对象。')
        else:
            notes.append('观察仅支持其有效时间和可见范围；朝向、操作及说话对应可助判断对象，单独看向镜头不证明交往。面对设备的自然招呼无需先知道姓名。')
    return '\n'.join(notes)
