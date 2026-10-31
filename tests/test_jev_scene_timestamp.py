from types import SimpleNamespace
import json
import pytest
from jshi.voice.jev_scene import JEVSceneStore,JEVSceneWriter

def test_copied_scene_timestamp_is_diagnostic_only(tmp_path):
    store=JEVSceneStore(tmp_path/'scene.json')
    store.append('one',{'text':'匠石啊','at':1000})
    snapshot=store.snapshot()
    raw=json.dumps({'scene':'有人称呼匠石，尚无后文。','scene_at':9999999999999,'discard_history':[]})
    writer=JEVSceneWriter(SimpleNamespace(generate=lambda request:SimpleNamespace(text=raw)))
    scene,_=writer.run('test',snapshot,'')
    assert snapshot['model_raw']==raw
    assert snapshot['ignored_output_fields']=={'scene_at':9999999999999}
    assert store.commit(snapshot,scene)
    assert store.state['revisions'][-1]['at'] != 9999999999999
    assert store.state['revisions'][-1]['events'][0]['payload']['at']==1000

def test_other_extra_fields_still_rejected(tmp_path):
    store=JEVSceneStore(tmp_path/'scene.json')
    raw=json.dumps({'scene':'现场','scene_at':0,'control':'stop','discard_history':[]})
    writer=JEVSceneWriter(SimpleNamespace(generate=lambda request:SimpleNamespace(text=raw)))
    with pytest.raises(ValueError):writer.run('test',store.snapshot(),'')
