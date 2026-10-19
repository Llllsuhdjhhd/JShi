import pytest
from tests.test_voice import process

from jshi.voice.personal_train import training_record_weights


def test_device_examples_keep_half_of_negative_weight_as_public_pool_grows():
    for ordinary_count in (2, 2000):
        rows=[{'label':1,'source':'manual'} for _ in range(3)]
        rows += [{'label':0,'source':'public','device_negative':True} for _ in range(4)]
        rows += [{'label':0,'source':'public'} for _ in range(ordinary_count)]
        weights=training_record_weights(rows)
        assert sum(weights[:3])==pytest.approx(1.)
        assert sum(weights[3:7])==pytest.approx(.5)
        assert sum(weights[7:])==pytest.approx(.5)


def test_explicit_local_tv_has_priority_over_a_large_downloaded_pool():
    for public_count in (2, 513, 5000):
        rows=[{'label':1,'source':'manual'}]
        rows += [{'label':0,'source':'manual','device_negative':True,
                  'local_device_negative':True} for _ in range(3)]
        rows += [{'label':0,'source':'public','device_negative':True} for _ in range(public_count)]
        rows += [{'label':0,'source':'public'} for _ in range(100)]
        weights=training_record_weights(rows)
        assert sum(weights[1:4])==pytest.approx(.375)
        assert sum(weights[4:4+public_count])==pytest.approx(.125)
        assert sum(weights[4+public_count:])==pytest.approx(.5)


@pytest.mark.parametrize('device', [False,True])
def test_a_single_negative_family_keeps_full_negative_weight(device):
    rows=[{'label':1,'source':'manual'}, {'label':1,'source':'voiceprint'},
          {'label':0,'source':'manual','device_negative':device}]
    assert training_record_weights(rows)==pytest.approx([.5,.175,1.])


def test_tv_import_is_deduplicated_negative_only_and_grouped(process,tmp_path):
    import io,wave
    import numpy as np
    from jshi.voice.device_corpus import import_tv_records
    from jshi.voice.personal import PersonalSpeakers
    from jshi.voice.personal_model import partition
    from tests.test_personal_speakers import speakers,audio
    buf=io.BytesIO()
    with wave.open(buf,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
        values=(np.sin(np.arange(16000*60)*.1)*12000).astype('<i2')
        w.writeframes(values.tobytes())
    records=[{'audio':{'path':'programme.wav','bytes':buf.getvalue()}}]
    service=PersonalSpeakers(tmp_path/'personal',process.profiles)
    try:
        before=len(process.profiles.list())
        report=import_tv_records(records,service.store,shard='test')
        assert report['added']==12 and report['original_recordings']==1
        assert import_tv_records(records,service.store,shard='test')['added']==0
        rows=service.store.background_samples()
        assert len({r['session_id'] for r in rows})==1
        assert all(partition(r['session_id'])=='train' for r in rows)
        assert len(process.profiles.list())==before
        service.store.save(audio(), 'lux-id','own','positive',source='manual')
        snapshot=service._records('lux-id',speakers())
        tv=[r for r in snapshot['records'] if r.get('device_negative')]
        assert len(tv)==12 and all(r['label']==0 for r in tv)
        assert service.dataset_progress('lux-id')['negative_device']==12
    finally:service.close()
