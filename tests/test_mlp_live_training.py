from threading import Event

import pytest

from jshi.voice.personal import PersonalSpeakers
from jshi.voice.personal_store import OTHER_VOICE
from tests.test_voice import process
from tests.test_personal_speakers import publish,audio,speakers


def test_label_correction_and_background_training_keep_old_scores_and_backup(process,tmp_path,monkeypatch):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    encoder=speakers();started=Event();release=Event()
    try:
        engine.select_targets(['lux-id'])
        sid=engine.store.save(audio(),'lux-id','s','i',source='manual')
        old=publish(engine,'lux-id',tmp_path,6)
        engine.store.relabel(sid,OTHER_VOICE)
        def train(oid,s):
            started.set()
            assert release.wait(5)
            publish(engine,oid,tmp_path,4)
            with engine.lock:engine.training.discard(oid)
        monkeypatch.setattr(engine,'_train',train)
        assert engine.request_training('lux-id',encoder,force=True)
        assert started.wait(2)
        current=engine.predict(audio(),encoder)
        assert current['ranking'][0]['version']==old['version']
        assert current['ranking'][0]['stale_labels'] and not current['matched']
        status=engine.status()['targets'][0]
        assert status['available'] and status['reference_only']
        release.set()
        engine.trainer.submit(lambda:None).result(timeout=5)
        versions=engine.manifest('lux-id')
        assert versions['active']['version']!=old['version']
        assert versions['previous']['version']==old['version']
        assert engine.predict(audio(),encoder)['matched']=='lux-id'
        engine.rollback('lux-id')
        restored=engine.predict(audio(),encoder)
        assert restored['ranking'][0]['version']==old['version'] and not restored['matched']
    finally:
        release.set();engine.close()


def test_new_labels_still_prevent_publishing_an_outdated_training_candidate(process,tmp_path):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    try:
        engine.select_targets(['lux-id'])
        sid=engine.store.save(audio(),'lux-id','s','i',source='manual')
        old=publish(engine,'lux-id',tmp_path,6)
        snapshot={'object_id':'lux-id','model_id':old['model_id'],'dimension':old['dimension'],
            'revision':old['revision'],'label_epoch':old['label_epoch'],'config':engine.store.settings()}
        engine.store.relabel(sid,OTHER_VOICE)
        from pathlib import Path
        import shutil
        output=tmp_path/'candidate.npz'
        shutil.copyfile(engine._directory('lux-id')/(old['version']+'.npz'),output)
        with pytest.raises(ValueError,match='标签已修正'):
            engine.publish('lux-id',snapshot,output,old)
        assert engine.manifest('lux-id')['active']['version']==old['version']
        assert engine.predict(audio(),speakers())['ranking']
    finally:engine.close()
