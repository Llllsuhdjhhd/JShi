from jshi.simplified import simplify_text, simplify_data
from jshi.skill.base import parse_json_object

def test_owned_prose_converts_but_ids_keys_urls_code_and_arguments_survive():
    value={'reply':'聲紋已確認，請繼續。','object_id':'人物-聲紋',
           'levels':{'繁體鍵':{'text':'記憶內容','event_ids':['事件-聲紋']}},
           'arguments':{'text':'必須原樣','path':'C:/資料/原檔.txt'}}
    result=simplify_data(value)
    assert result['reply']=='声纹已确认，请继续。'
    assert result['object_id']==value['object_id']
    assert result['levels']['繁體鍵']['text']=='记忆内容'
    assert result['levels']['繁體鍵']['event_ids']==['事件-聲紋']
    assert result['arguments']==value['arguments']
    assert simplify_text('這是 `繁體變量`，請看 https://example.com/聲紋')=='这是 `繁體變量`，请看 https://example.com/聲紋'
    assert simplify_text(r'開啟 C:\資料\檔案.txt')==r'开启 C:\資料\檔案.txt'

def test_parser_normalizes_prose_without_changing_numeric_or_routing_contract():
    original='{"reply":"請繼續","level":2,"route":"main","decision_refs":["I1"]}'
    assert parse_json_object(original)=={'reply':'请继续','level':2,'route':'main','decision_refs':['I1']}
    assert '請繼續' in original

def test_pending_index_updates_changed_event_and_parent_then_clears_marker(tmp_path):
    import json
    from types import SimpleNamespace
    from jshi.memory.rems3 import Rems3MemoryBackend
    from jshi.memory.rems3 import load_rems
    load_rems()
    from rems.models.event import Event
    marker=tmp_path/'pending.json';marker.write_text(json.dumps(['e']),encoding='utf-8')
    events=[Event(event_id='grandparent',content_raw='上层事件',source_events=['parent']),
            Event(event_id='e',content_raw='原始事件'),
            Event(event_id='parent',content_raw='父事件',source_events=['e']),
            Event(event_id='other',content_raw='其他事件')]
    assert not hasattr(events[0], 'recall_metadata')
    seen=[]
    backend=Rems3MemoryBackend.__new__(Rems3MemoryBackend)
    backend._simplified_pending_path=marker;backend._semantic_disabled=False
    backend._pipeline=SimpleNamespace(event_repo=SimpleNamespace(list_all=lambda **kw:events),
        recall_pipeline=SimpleNamespace(index_event=lambda event:seen.append(event.event_id)))
    backend._finish_simplified_reindex()
    assert seen==['grandparent','e','parent']
    assert not marker.exists()

def test_failed_index_update_keeps_retry_marker(tmp_path):
    import json,pytest
    from types import SimpleNamespace
    from jshi.memory.rems3 import Rems3MemoryBackend
    from jshi.memory.rems3 import load_rems
    load_rems()
    from rems.models.event import Event
    marker=tmp_path/'pending.json';marker.write_text(json.dumps(['e']),encoding='utf-8')
    backend=Rems3MemoryBackend.__new__(Rems3MemoryBackend)
    backend._simplified_pending_path=marker;backend._semantic_disabled=False
    def fail(event):raise RuntimeError('write failed')
    backend._pipeline=SimpleNamespace(event_repo=SimpleNamespace(list_all=lambda **kw:[
        Event(event_id='e',content_raw='原始事件')]),recall_pipeline=SimpleNamespace(index_event=fail))
    with pytest.raises(RuntimeError):backend._finish_simplified_reindex()
    assert marker.exists()
