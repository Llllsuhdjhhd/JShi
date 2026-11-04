from jshi.recognition.acquaintance import AcquaintanceStore
import pytest


def test_encounters_names_and_questions_survive_restart_without_inventing_facts(tmp_path):
    p=tmp_path/'people.db'
    a=AcquaintanceStore(p)
    a.record('s','visit1','a','encounter',{'session':'one','text':'你好'})
    a.record('s','visit1','a','encounter',{'session':'one','text':'重复'})
    a.record('s','q','a','name_question_played',{'text':'怎么称呼？'})
    a.record('s','n','a','reported_name',{'name':'小陈'})
    a.record('s','visit2','a','encounter',{'session':'two'})
    b=AcquaintanceStore(p)
    assert len(b.events('s','a'))==4
    assert b.context('s','a')['reported_name']=='小陈'
    assert b.context('s','a')['name_asked']
    assert b.context('s','a')['recorded_encounters']==2
    assert not b.context('s','b')['name_asked']
    assert not b.events('other','a')


def test_links_are_scoped_idempotent_retryable_and_revocable(tmp_path):
    a=AcquaintanceStore(tmp_path/'people.db')
    with pytest.raises(ValueError):
        a.link('s','anon','lux',['i1'],basis='self_report')
    k=a.link('s','anon','lux',['i1'],basis='manual_annotation',modules=['memory'],link_id='k')
    assert a.link('s','anon','lux',['i1'],basis='manual_annotation',modules=['memory'],link_id='k')==k
    assert a.resolve('s','anon','i1')=='lux'
    assert a.resolve('s','anon','i2')=='anon'
    assert a.resolve('other','anon','i1')=='anon'
    def fail(row): raise RuntimeError('retry')
    a.apply(k,{'memory':fail})
    calls=[]
    a.apply(k,{'memory':lambda row:calls.append(row['active'])})
    a.apply(k,{'memory':lambda row:calls.append(row['active'])})
    assert calls==[1]
    a.revoke(k)
    assert a.resolve('s','anon','i1')=='anon'
    a.apply(k,{'memory':lambda row:calls.append(row['active'])})
    assert calls==[1,0]


def test_dialogue_projection_preserves_source_and_can_be_revoked(tmp_path):
    from jshi.voice.dialogue import VoiceDialogue
    d=VoiceDialogue(tmp_path/'dialogue.json')
    d.input('s','i1','你好','临时人物A',actor_id='a')
    d.input('s','i2','旁人说的','临时人物A',actor_id='a')
    d.link_inputs('s',['i1'],'lux','lux',link_id='one')
    assert d.recent('s')[0]['who']=='lux'
    assert d.recent('s')[1]['who']=='临时人物A'
    assert d.state['inputs'][0]['who']=='临时人物A'
    d.link_inputs('s',['i1'],'lux','lux',link_id='one',active=False)
    assert d.recent('s')[0]['who']=='临时人物A'


def test_self_report_persists_without_merging_same_name(process):
    from jshi.voice.identity import VoiceIdentities
    from tests.test_voice import process as unused
    ids=VoiceIdentities(process.profiles,'stone','one',process.repository)
    old=ids.resolve('a',cluster_id='voice-a')
    named,_=ids.introduce('a','lux',cluster_id='voice-a')
    assert named.object_id==old.object_id and named.object_id!='lux-id'
    again=VoiceIdentities(process.profiles,'stone','two',process.repository)
    assert again.acquaintance.context('stone',old.object_id)['reported_name']=='lux'
    assert process.profiles.get(old.object_id).label == 'lux'


from tests.test_voice import process


def test_spoken_name_keeps_voice_and_memories_across_restart(process):
    from jshi.voice.identity import VoiceIdentities
    from jshi.recognition import CarrierEntry
    from jshi.core.person_names import known_names
    ids = VoiceIdentities(process.profiles, 'stone', 'first', process.repository)
    old = ids.resolve('a', cluster_id='a')
    process.profiles.add_carrier(old.object_id, CarrierEntry('voiceprint', 'local:test:ning'))
    ids.name_question_played(old.object_id, '我该怎么称呼你？')
    name = ids.name_answer(old, '小宁就好。')
    assert name == '小宁'
    named, _ = ids.introduce('a', name, basis='name_answer', cluster_id='a')
    restarted = VoiceIdentities(process.profiles, 'stone', 'second', process.repository)
    matched = restarted.resolve('b', voiceprint_id='local:test:ning', confidence=.91)
    assert matched.object_id == named.object_id == old.object_id
    assert matched.label == '小宁' and matched.method == 'voiceprint_match'
    assert matched.confidence == .91
    assert known_names({'P1': old.object_id}, process.profiles) == {'P1': '小宁'}
    event = restarted.acquaintance.events('stone', old.object_id)[-1]
    assert event['payload']['status'] == 'self_reported'
    corrected, _ = restarted.introduce('b', '小林')
    assert corrected.object_id == old.object_id
    assert restarted.resolve('c', voiceprint_id='local:test:ning').label == '小林'


def test_same_named_voices_remain_separate_and_duplicates_are_recorded(process):
    from jshi.voice.identity import VoiceIdentities
    from jshi.recognition import CarrierEntry
    from jshi.core.person_names import known_names
    ids = VoiceIdentities(process.profiles, 'stone', 'names', process.repository)
    people = []
    for key in ('a', 'b', 'c'):
        old = ids.resolve(key, cluster_id=key)
        process.profiles.add_carrier(old.object_id, CarrierEntry('voiceprint', 'local:test:'+key))
        named, _ = ids.introduce(key, '小明', cluster_id=key)
        people.append(named.object_id)
    assert len(set(people)) == 3
    assert known_names(dict(zip(('P1','P2','P3'), people)), process.profiles) == {}
    for key, oid in zip(('a','b','c'), people):
        assert ids.resolve('new-'+key, voiceprint_id='local:test:'+key).object_id == oid
    record = ids.acquaintance.events('stone', people[-1])[-1]
    assert set(record['payload']['same_name_people']) == set(people[:2])
