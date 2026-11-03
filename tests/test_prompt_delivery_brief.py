from datetime import timedelta
from jshi.voice.dialogue import delivery_brief
from jshi.tool.service import select_tool_related_entries
from jshi.tool.contract import utc_now


def test_delivery_projects_facts_without_transport_ids():
    text = delivery_brief({'reply_id':'long-secret','targets':[['OBJ-1']]*4,
        'played_text':'已经说完。','partial_text':'正在说的句子。','pending_text':'尚未说。',
        'reason':'connection closed'})
    assert 'OBJ-' not in text and 'long-secret' not in text
    assert '已播｜匠石：已经说完。' in text
    assert '部分播放（具体进度未知）' in text and '未播｜' in text


def test_old_unrelated_open_work_is_not_repeated_but_related_work_can_return():
    now = utc_now()
    item = {'section':'recent','work_open':True,'updated_at':now-timedelta(days=14),
        'used_at':now-timedelta(days=14),'need':'banana milk supermarket','tool_name':'search'}
    assert not select_tool_related_entries([item], query='geometry algebra homework',now=now,ttl_s=1)
    assert select_tool_related_entries([item],query='banana milk supermarket',now=now,ttl_s=1)
