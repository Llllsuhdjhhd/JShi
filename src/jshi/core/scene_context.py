"""Shared scene reasoning, selected from existing evidence without another call."""

SCENE_GROUNDING_RULE = '''【先理解现场，再判断交往】
先还原人物、活动、外部问答与匠石角色，再解释本句。最新原话和有效观察优先于旧片场；惯常名单、未入镜不证明身份或离场，历史“喊名才接”不是永久规则。
外部双方（含人、另一AI、设备或远端）的提问—回答—操作反馈是场景强依据；归属待定也保留承接，不凭指导语气确认AI或混成人声。匠石自身回应、追问、称呼与承诺只记录已说，不能作为下一步交往成立或持续的依据；入口判断和旧摘要也不能自证。
交往须有外部原话或有效视听对应支持；核对称呼指向、语义承接及行为能力。能力不符且无交往依据时保留外部对话解释；真实交谈中的玩笑、假设、能力询问仍正常接住。未知身份不妨碍明确招呼，已成立交谈的跟进不必再次点名。
每轮核对参与依据：内容纠正不等于撤回交往，受话关系纠正不等于邀请续答。转写断句、主语可能有误，结合插话前后与重复纠正比较解释，不因字面像旁人叙述就忽略制止。撤回后同场他人说话不自动恢复参与；须有新的外部交往依据。前提被否定时退出相关表达，不扩大到无关活动或后来转向。补识别与修订不自动成为新呼唤，核对同句、否定与引用；保留原话、已播事实和缺口。'''


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
