from dataclasses import replace
from jshi.voice.contracts import Transcript
from jshi.voice.utterance_organizer import UtteranceOrganizer


def row(text, start, end, who='A'):
    return Transcript(text, who, start, end, True, voiceprint_id=who)


def test_interruption_and_resumption_preserve_half_sentence_and_other_person():
    organizer = UtteranceOrganizer()
    organizer.observe([row('我要去', 0, 1000)])
    changed = organizer.observe([row('我没空', 1100, 2000, 'B')])
    a = next(r for r in changed if r['utterance_id'] == 'U1')
    assert a['state'] == 'interrupted' and a['interrupted_by'] == ['U2']
    changed = organizer.observe([row('买鸡蛋', 2100, 3000)])
    a = next(r for r in changed if r['utterance_id'] == 'U1')
    assert [r['text'] for r in a['parts']] == ['我要去', '买鸡蛋']
    assert a['interrupted_by'] == ['U2']
    assert a['state'] != 'complete'


def test_revision_updates_existing_record_and_duplicate_does_not_increment():
    organizer = UtteranceOrganizer()
    original = row('手机', 0, 1000)
    organizer.observe([original])
    assert organizer.observe([original]) == []
    updates = organizer.observe([replace(original, text='电脑', replaces=((0, 1000),))])
    assert len(organizer.records) == 1
    assert updates[0]['version'] == 2
    assert updates[0]['parts'][0]['text'] == '电脑'


def test_unknown_track_does_not_bind_two_people_and_records_are_bounded():
    organizer = UtteranceOrganizer(max_records=2)
    for i in range(3):
        organizer.observe([row('嗯', i*1000, i*1000+500, '')])
    assert len(organizer.records) == len(organizer.sources) == 2
    assert all(r.speaker is None for r in organizer.records.values())


def test_organized_half_sentence_reaches_actual_split_jev_request():
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry
    answer = entry(route='background',interaction='none',control='keep',process_refs=[])
    answer['items'][0].update(person='unknown',certainty='unknown')
    model = Model([answer])
    VoiceJEV(model,mode='split',fallback_models=[]).decide_batch('s',[
        {'n':1,'who':'unknown','text':'鸡蛋也买几个','candidates':[]}],[],
        {'utterance_context':[{'utterance':'U1','person':'声音1','state':'interrupted',
            'interrupted_by':['U2'],'text':'我要去商店'}]})
    assert '我要去商店' in model.requests[0].input_text
    assert 'interrupted' in model.requests[0].input_text
