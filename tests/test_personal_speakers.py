from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import replace
import json
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from jshi.app.voice import VoiceConversation, create_app
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.recognition import ObjectProfile
from jshi.voice.config import VoiceConfig
from jshi.voice.local import LocalSpeakers
from jshi.voice.personal import PersonalSpeakers
from jshi.voice.personal_model import WIDTHS, batch_scores, clip_scores, partition, split_pcm
from jshi.voice.personal_store import PersonalStore
from jshi.voice.personal_train import groups, train
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, process


def audio(seconds=.5, value=1000):
    return np.full(round(seconds*16000), value, dtype='<i2').tobytes()


class Extractor:
    def __init__(self):
        self.lengths = []
    def create_stream(self):
        obj = SimpleNamespace()
        obj.accept_waveform = lambda sample_rate, waveform: setattr(obj, 'values', waveform)
        obj.input_finished = lambda: None
        return obj
    def is_ready(self, stream):
        return True
    def compute(self, stream):
        self.lengths.append(len(stream.values))
        return [1., 0.] if stream.values.mean() > 0 else [0., 1.]


def speakers():
    return SimpleNamespace(extractor=Extractor(), lock=RLock(), model_id='fixture-encoder')


def weights(bias=0, seed=0):
    rng, result, width = np.random.default_rng(seed), {}, 2
    for i, end in enumerate((*WIDTHS, 1), 1):
        result[f'w{i}'] = rng.normal(0, .02, (width, end)).astype('float32')
        result[f'b{i}'] = np.zeros(end, dtype='float32')
        width = end
    if bias:
        result['w4'][:] = 0
        result['b4'][:] = bias
    return result


def publish(service, oid, tmp_path, bias):
    path = tmp_path / ('candidate-' + oid + '.npz')
    np.savez(path, **weights(bias))
    revision, epoch = service.store.revision()
    snapshot = {'object_id': oid, 'model_id': 'fixture-encoder', 'dimension': 2,
                'revision': revision, 'label_epoch': epoch}
    result = {'threshold': .8, 'validation': {'positive': 4, 'negative': 12, 'recall': 1., 'false_accept': 0.},
              'calibration': {}, 'counts': {}, 'duration_reports': {key: {'enabled': True}
                  for key in ('under_1s', '1_to_3s', '3s_plus')}}
    service.publish(oid, snapshot, path, result)
    return service.manifest(oid)['active']


def test_roster_budget_pins_and_independent_label_precedence(tmp_path):
    store = PersonalStore(tmp_path)
    store.configure({'budget': 2})
    store.select(['a'])
    store.select(['b'], source='jiangshi')
    assert {r['object_id'] for r in store.targets()} == {'a', 'b'}
    with pytest.raises(ValueError, match='预算'):
        store.select(['b', 'c'], source='jiangshi')
    with pytest.raises(ValueError):
        store.configure({'budget': True})
    sid = store.save(audio(.3), 'a', 's', 'i', source='voiceprint', score=.9)
    store.save(audio(.3), 'b', 's', 'i', source='manual')
    epoch = store.revision()[1]
    store.save(audio(.3), 'a', 's', 'i', source='voiceprint', score=.99)
    assert store.samples()[0]['object_id'] == 'b' and store.samples()[0]['source'] == 'manual'
    assert store.revision()[1] == epoch
    with pytest.raises(ValueError, match='重叠'):
        store.save(audio(), 'a', 's', 'j', source='manual', overlap=True)
    with pytest.raises(ValueError, match='门槛'):
        store.save(audio(), 'a', 's', 'j', source='voiceprint', score=.49)
    store.delete(sid)
    assert store.revision()[1] > epoch


@pytest.mark.parametrize('seconds,count', [(.3, 1), (6, 1), (12, 2), (30, 3), (90, 3)])
def test_segmentation_covers_original_once(seconds, count):
    pcm = audio(seconds)
    pieces = split_pcm(pcm)
    assert len(pieces) == count and b''.join(pieces) == pcm
    assert all(len(piece) >= 6400 for piece in pieces)


def test_batched_mlp_matches_independent_forward():
    x = np.array([[1., 0.], [0., 1.]], dtype='float32')
    models = [weights(seed=i) for i in range(3)]
    expected = []
    for model in models:
        y = x
        for i in range(1, 5):
            y = y @ model[f'w{i}'] + model[f'b{i}']
            if i < 4:
                y = np.maximum(y, 0)
        expected.append(1/(1+np.exp(-y[:, 0])))
    np.testing.assert_allclose(batch_scores(x, models), expected, atol=1e-7)
    from jshi.voice.personal_model import pack_weights
    np.testing.assert_allclose(batch_scores(x, models, packed=pack_weights(models)), expected, atol=1e-7)


def test_short_features_no_repeat_original_wrapper_unchanged(process, tmp_path):
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    s = speakers()
    try:
        vectors, duration = engine.make_features(audio(.3), s)
        assert s.extractor.lengths == [4800] and duration == [.3] and len(vectors[0]) == 2
        original = LocalSpeakers(s.extractor, 'fixture', tmp_path/'original.json', process.profiles)
        assert original.embedding(np.ones(4800)) is None
        sid = engine.capture(audio(.3), 'lux-id', 's', 'i', None, source='manual')
        assert engine.store.samples(audio=True)[0]['id'] == sid
        assert engine.store.features(sid, s.model_id) is None
    finally:
        engine.close()


def test_publish_rollback_conflicts_feature_versions_and_correction(process, tmp_path):
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    s = speakers()
    try:
        engine.select_targets(['lux-id', 'other'])
        first = publish(engine, 'lux-id', tmp_path, 6)
        other = publish(engine, 'other', tmp_path, -6)
        assert engine.predict(audio(.3), s)['matched'] == 'lux-id'
        s.model_id = 'different'
        assert engine.predict(audio(), s)['status'] == 'no_ready_models'
        s.model_id = 'fixture-encoder'
        second = publish(engine, 'lux-id', tmp_path, 5)
        assert engine.manifest('other')['active'] == other
        assert engine.manifest('lux-id')['previous'] == first
        engine.rollback('lux-id')
        assert engine.manifest('lux-id')['active']['version'] == first['version']
        assert engine.manifest('lux-id')['previous']['version'] == second['version']
        publish(engine, 'other', tmp_path, 6)
        assert engine.predict(audio(), s)['matched'] == ''
        sid = engine.store.save(audio(), 'lux-id', 's', 'i', source='manual')
        engine.store.relabel(sid, 'other')
        result=engine.predict(audio(),s)
        assert result['ranking'] and not result['matched']
        assert all(r['stale_labels'] and not r['verified'] for r in result['ranking'])
        engine.rollback('lux-id')
        assert engine.predict(audio(),s)['ranking']
    finally:
        engine.close()


def test_auto_collection_uses_only_current_original_voiceprint(process, tmp_path, monkeypatch):
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    engine.store.configure({'auto_score': .85})
    engine.select_targets(['lux-id'])
    calls = []
    monkeypatch.setattr(engine, 'submit_capture', lambda *args, **kw: calls.append(kw))
    try:
        t = Transcript('测试', 'A', 0, 500, True, confidence=.95, input_id='i')
        e = SpeakerEvidence('A', 'lux-id', 'lux', 'recognized', 'voiceprint_match')
        engine.collect_original(audio(), t, e, 's', speakers())
        assert len(calls) == 1 and calls[0]['source'] == 'voiceprint'
        for text, evidence in [(replace(t, identity_note=json.dumps({'collection': {'current_score': .6}})), e),
                               (replace(t, confidence=None), e), (replace(t, overlap=True), e),
                               (t, replace(e, method='personal_classifier'))]:
            engine.collect_original(audio(), text, evidence, 's', speakers())
        assert len(calls) == 1
    finally:
        engine.close()


@pytest.mark.parametrize(('score', 'runner', 'source', 'expected'), [
    (.555, .20, 'voiceprint', True), (.522, .25, 'voiceprint', True),
    (.50, .40, 'voiceprint', True), (.499, .10, 'voiceprint', False),
    (.555, .49, 'voiceprint', False), (.80, .20, 'repeated_short_audio', False),
    (.98, .20, 'personal_classifier', False),
])
def test_auto_collection_score_and_lead_policy(process, tmp_path, monkeypatch, score, runner, source, expected):
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    calls = []
    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return Future()
    monkeypatch.setattr(engine, 'submit_capture', capture)
    try:
        assert not engine.store.targets()  # Storing samples does not require choosing all model targets first.
        t = Transcript('真实原声纹排名', 'A', 0, 2000, True, input_id='i')
        ranking = [{'object_id': 'lux-id', 'score': score, 'source': source},
                   {'object_id': 'other', 'score': runner, 'source': source}]
        job = engine.collect_original(audio(2), t, SpeakerEvidence('A'), 's', speakers(), candidates=ranking)
        assert bool(job) is expected
        assert len(calls) == int(expected)
        if job:
            assert job['object_id'] == 'lux-id'
            assert calls[0][1]['source'] == 'voiceprint'
    finally:
        engine.close()


def test_auto_default_upgrade_preserves_custom_threshold(tmp_path):
    for old_score, expected in ((.85, .50), (.70, .70)):
        store = PersonalStore(tmp_path/str(old_score))
        with store.connect() as db:
            config = store.value(db, 'settings')
            config.pop('auto_margin')
            config['auto_score'] = old_score
            store.put(db, 'settings', config)
        upgraded = PersonalStore(store.root).settings()
        assert upgraded['auto_score'] == expected
        assert upgraded['auto_margin'] == .10


def test_saved_auto_collection_publishes_check_and_can_be_withdrawn(process, tmp_path):
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    async def run():
        sent = []
        async def send(value): sent.append(value)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, personal_speakers=engine)
        try:
            t = Transcript('自动收录声音', 'A', 0, 2000, True, input_id='auto')
            provisional = SpeakerEvidence('A', 'lux-id', 'lux', 'provisional', 'jev_attribution')
            c.input_records['auto'] = {'pcm': audio(2), 'transcript': t}
            c.annotations['auto'] = provisional
            c.scene.append(SceneUtterance(t.text, provisional, 0, 2000, input_id='auto'))
            job = engine.collect_original(audio(2), t, SpeakerEvidence('A'), c.session_id, speakers(),
                candidates=[{'object_id': 'lux-id', 'score': .555}, {'object_id': 'other', 'score': .20}])
            assert not sent  # No optimistic check before persistence completes.
            await c._publish_personal_auto('auto', job)
            assert sent[-1]['type'] == 'personal_auto_labelled'
            assert engine.store.samples()[0]['source'] == 'voiceprint'
            assert c.annotations['auto'] == provisional
            await c.unlabel_personal_inputs(['auto'])
            assert sent[-1]['type'] == 'personal_unlabelled'
            assert sent[-1]['restored'][0]['identity_unchanged'] is True
            assert engine.store.samples() == []
            assert c.annotations['auto'] == provisional and c.scene[-1].speaker == provisional
            assert engine.capture(audio(2), 'lux-id', c.session_id, 'auto', speakers(),
                                  source='voiceprint', score=.555) is None
        finally:
            await c.close()
    try:
        asyncio.run(run())
    finally:
        engine.close()


def make_training_records():
    records = []
    for kind in ('train', 'calibration', 'validation'):
        sessions = [f'{kind}-{i}' for i in range(100) if partition(f'{kind}-{i}') == kind][:4]
        for i, session in enumerate(sessions):
            for label in (0, 1):
                records.append({'id': f'{session}-{label}', 'session_id': session, 'source': 'manual',
                    'label': label, 'vectors': [[float(label), float(1-label)]], 'durations': [.5]})
    return records


def test_actual_training_has_disjoint_calibration_validation_and_warm_start(tmp_path):
    pytest.importorskip('torch')
    records = make_training_records()
    splits = groups(records)
    group_sets = [{r['session_id'] for r in splits[k]} for k in ('train', 'calibration', 'validation')]
    assert all(not a & b for i, a in enumerate(group_sets) for b in group_sets[i+1:])
    from jshi.voice.personal_store import DEFAULTS
    snapshot = {'records': records, 'dimension': 2, 'config': {**DEFAULTS,
        'min_train_positive': 2, 'min_train_negative': 2, 'min_eval_positive': 2, 'min_eval_negative': 2}}
    path = tmp_path/'snapshot.json'
    path.write_text(json.dumps(snapshot), encoding='utf-8')
    result = train(path, tmp_path/'first.npz')
    assert result['status'] == 'ready', result
    assert result['validation']['false_accept'] == 0 and result['validation']['recall'] == 1
    snapshot['previous'] = {'path': str(tmp_path/'first.npz'), 'threshold': result['threshold']}
    path.write_text(json.dumps(snapshot), encoding='utf-8')
    result2 = train(path, tmp_path/'second.npz')
    assert result2['status'] == 'ready', result2
    # No noisy auto-labelled recording can serve as calibration or independent validation.
    altered = [{**r, 'source': 'voiceprint'} for r in records]
    assert not groups(altered)['validation'] and not groups(altered)['calibration']


def test_web_targets_short_label_and_origin_guard(process, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    import base64
    engine, s = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    async def run():
        client = TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''),
                                                   online_speakers=s, personal_speakers=engine)))
        await client.start_server()
        try:
            origin = {'Origin': str(client.make_url('/')).rstrip('/')}
            response = await client.post('/api/personal-speakers', json={'action': 'target', 'object_id': 'lux-id'})
            assert response.status == 403
            response = await client.post('/api/personal-speakers', json={'action': 'target', 'object_id': 'lux-id'}, headers=origin)
            assert response.status == 200, await response.text()
            response = await client.post('/api/personal-speakers', json={'action': 'sample', 'object_id': 'lux-id',
                'session_id': 'recording', 'input_id': 'short', 'pcm': base64.b64encode(audio(.3)).decode()}, headers=origin)
            assert response.status == 200, await response.text()
            assert engine.store.samples()[0]['seconds'] == .3
            assert (await client.get('/personal-speakers')).status == 200
            assert (await client.get('/voice-enroll')).status == 200
        finally:
            await client.close()
    asyncio.run(run())


def test_one_click_training_selects_person_and_bypasses_batch_only(process, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    engine, s = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    engine.store.configure({'batch_samples': 10000, 'budget': 1})
    async def run():
        client = TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''),
                                                  online_speakers=s, personal_speakers=engine)))
        await client.start_server()
        try:
            origin = {'Origin': str(client.make_url('/')).rstrip('/')}
            assert not engine.store.targets()
            r = await client.post('/api/personal-speakers', headers=origin, json={
                'action': 'train', 'object_id': 'lux-id', 'select_target': True})
            assert r.status == 200, await r.text()
            assert (await r.json())['queued'] is True
            await asyncio.to_thread(lambda: engine.trainer.submit(lambda: None).result(timeout=10))
            target = engine.store.targets()[0]
            assert target['object_id'] == 'lux-id' and target['pinned']
            assert target['status'] == 'collecting'  # Force does not bypass the validation data minimum.
            process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
            r = await client.post('/api/personal-speakers', headers=origin, json={
                'action': 'train', 'object_id': 'other', 'select_target': True})
            assert r.status == 400 and '预算' in await r.text()
            engine.store.configure({'enabled': False})
            r = await client.post('/api/personal-speakers', headers=origin, json={
                'action': 'train', 'object_id': 'lux-id', 'select_target': True})
            assert (await r.json())['detail'] == '个人声音学习已暂停'
        finally:
            await client.close()
    asyncio.run(run())


def test_conversation_mlp_is_independent_and_preserves_reliable_identity(process):
    class Auxiliary:
        def __init__(self):
            self.called = 0
        def submit_predict(self, pcm, speakers):
            self.called += 1
            future = Future()
            future.set_result({'status': 'matched', 'matched': 'lux-id', 'ranking': [
                {'object_id': 'lux-id', 'label': 'lux', 'score': .98, 'threshold': .8}]})
            return future
    async def run():
        aux = Auxiliary()
        sent = []
        async def send(value):
            sent.append(value)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, personal_speakers=aux)
        try:
            t = Transcript('嗯', 'A', 0, 300, True, input_id='i')
            c.input_records['i'] = {'pcm': audio(.3)}
            original = SpeakerEvidence('A', 'lux-id', 'lux', 'recognized', 'voiceprint_match', .95)
            assert await c.personal_identity(t, original) == original and aux.called == 1
            assert c.personal_prediction_view(c.input_records['i'])['ranking'][0]['score'] == .98
            unknown = SpeakerEvidence('A', method='unassigned_audio')
            c.source_speakers['i'] = unknown
            result = await c.personal_identity(t, unknown)
            assert result.object_id == 'lux-id' and result.method == 'personal_classifier' and aux.called == 1
            view = c.personal_prediction_view(c.input_records['i'])
            assert view['ranking'][0]['score'] == .98 and view['ranking'][0]['label'] == 'lux'
            assert c.source_speakers['i'] == unknown
            u = SceneUtterance('嗯', result, 0, 300, input_id='i')
            evidence = c.voice_evidence(u, [])
            assert evidence['strength'] == 'clear' and '不是原声纹相似度' in evidence['basis']
        finally:
            await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('overlap', [False, True])
def test_actual_personal_model_outputs_reach_the_input_row(process, tmp_path, overlap):
    from jshi.voice.audio_buffer import SessionAudio
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    engine, s = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    engine.select_targets(['lux-id', 'other'])
    publish(engine, 'lux-id', tmp_path, 4)
    publish(engine, 'other', tmp_path, -4)
    async def run():
        sent = []
        async def send(value): sent.append(value)
        source = SessionAudio()
        source.append(audio(.5))
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=s,
                              sample_source=source, personal_speakers=engine, input_pause_s=.01)
        try:
            await c.accept(Transcript('本句调用个人模型', 'unidentified-A', 0, 500, True, overlap=overlap))
            await asyncio.wait_for(c.queue.join(), 3)
            event = next(m for m in sent if m['type'] == 'timeline')
            prediction = [m for m in sent if m['type'] == 'personal_prediction'
                          and m['input_id'] == event['input_id']][-1]['personal_prediction']
            assert prediction['status'] == 'matched'
            assert prediction['matched'] == ('' if overlap else 'lux-id')
            assert prediction['overlap'] is overlap
            if overlap:
                comparison = c.identity_comparison(event['input_id'])
                assert not comparison['mlp_high'] and not comparison['mlp_priority']
                assert event['speaker']['method'] != 'personal_classifier'
            rows = {row['object_id']: row for row in prediction['ranking']}
            assert rows['lux-id']['label'] == 'lux' and rows['lux-id']['score'] == pytest.approx(1/(1+np.exp(-4)))
            assert rows['other']['label'] == '其他人' and rows['other']['score'] == pytest.approx(1/(1+np.exp(4)))
            assert all('version' not in row and 'source' not in row for row in rows.values())
            assert c.source_speakers[event['input_id']].method != 'personal_classifier'
        finally:
            await c.close()
    try:
        asyncio.run(run())
    finally:
        engine.close()


def test_personal_output_survives_manual_correction_during_prediction(process):
    future = Future()
    auxiliary = SimpleNamespace(submit_predict=lambda *args: future)
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    async def run():
        async def send(value): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, personal_speakers=auxiliary)
        try:
            t = Transcript('本句实际计算结果', 'A', 0, 300, True, input_id='i')
            c.input_records['i'] = {'pcm': audio(.3)}
            pending = asyncio.create_task(c.personal_identity(t, SpeakerEvidence('A')))
            await asyncio.sleep(0)
            manual = SpeakerEvidence('A', 'other', '其他人', 'recognized', 'manual_annotation')
            c.annotations['i'] = manual
            future.set_result({'status': 'matched', 'matched': 'lux-id', 'ranking': [
                {'object_id': 'lux-id', 'label': 'lux', 'score': .92, 'threshold': .8}]})
            assert await pending == manual
            assert c.personal_prediction_view(c.input_records['i'])['ranking'][0]['score'] == .92
        finally:
            await c.close()
    asyncio.run(run())


def test_recomputed_voiceprint_ranking_reaches_ui_without_confirming_identity(process):
    async def run():
        events = []
        async def send(value):
            events.append(value)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.01)
        async def ranking(transcript, speaker):
            return [{'object_id': 'lux-id', 'label': 'lux', 'score': .127}], 'ranked'
        c.rank_candidates = ranking
        try:
            await c.accept(Transcript('背景里的声音', 'other-track', 0, 3000, True))
            await asyncio.wait_for(c.queue.join(), 3)
            event = next(e for e in events if e['type'] == 'timeline')
            assert event['identity_note'] == ''
            update = next(e for e in events if e['type'] == 'voiceprint_candidates')
            assert update['input_id'] == event['input_id'] and update['candidates'] == [
                {'object_id': 'lux-id', 'label': 'lux', 'score': .127, 'source': 'voiceprint'}]
            assert event['speaker']['status'] != 'recognized'
            assert event['speaker']['object_id'] != 'lux-id'
        finally:
            await c.close()
    asyncio.run(run())


def test_background_subprocess_trains_and_publishes_a_real_mlp(process, tmp_path):
    pytest.importorskip('torch')
    process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
    engine, s = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    try:
        engine.store.configure({'batch_samples': 10000, 'min_train_positive': 2,
            'min_train_negative': 2, 'min_eval_positive': 2, 'min_eval_negative': 2})
        engine.select_targets(['lux-id'])
        for row in make_training_records():
            engine.capture(audio(.5, 1000 if row['label'] else -1000),
                'lux-id' if row['label'] else 'other', row['session_id'], row['id'], s, source='manual')
        assert engine.request_training('lux-id', s, force=True)
        engine.trainer.submit(lambda: None).result(timeout=60)
        state = engine.status()['targets'][0]
        assert state['available'] and state['status'] == 'ready', state
        assert engine.predict(audio(.3), s)['matched'] == 'lux-id'
        assert not engine.predict(audio(.3, -1000), s)['matched']
    finally:
        engine.close()


def test_manual_short_annotation_independent_of_voiceprint_registration(process, tmp_path):
    engine, s = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    async def run():
        sent = []
        async def send(value):
            sent.append(value)
        original = LocalSpeakers(s.extractor, s.model_id, tmp_path/'original.json', process.profiles)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=original, personal_speakers=engine)
        try:
            t = Transcript('嗯', 'A', 0, 300, True, input_id='i')
            c.input_records['i'] = {'pcm': audio(.3), 'transcript': t}
            c.scene.append(SceneUtterance('嗯', SpeakerEvidence('A'), 0, 300, input_id='i'))
            await c.label_personal_inputs(['i'], 'lux-id')
            assert sent[-1]['type'] == 'personal_labelled', sent
            assert engine.store.samples()[0]['seconds'] == .3
            assert c.scene[-1].speaker.method == 'manual_annotation'
            assert c.voice_evidence(c.scene[-1], [])['basis'] == '用户人工确认本句归属'
            assert not original.known and not original.path.exists()
            # Repeated clicks upsert this recording; a failed row can be retried in place.
            await c.label_personal_inputs(['i'], 'lux-id')
            assert len(engine.store.samples()) == 1
            await c.label_personal_inputs(['expired-row'], 'lux-id')
            assert sent[-1]['type'] == 'personal_label_error'
            assert sent[-1]['input_ids'] == ['expired-row']
            assert sent[-1]['object_id'] == 'lux-id'
            process.profiles.create(ObjectProfile('other', '其他人', status='confirmed'))
            await c.label_personal_inputs(['i'], 'other')
            assert engine.store.samples()[0]['object_id'] == 'other'
            epoch = engine.store.revision()[1]
            await c.unlabel_personal_inputs(['i'])
            assert sent[-1]['type'] == 'personal_unlabelled', sent[-1]
            assert sent[-1]['restored'][0]['speaker']['method'] == 'unidentified'
            assert engine.store.samples() == []
            assert engine.store.revision()[1] > epoch
            assert 'i' not in c.annotations
            assert c.scene[-1].speaker.method == 'unidentified'
            assert not original.known and not original.path.exists()
            # A delayed automatic capture cannot put the withdrawn sample back.
            assert engine.capture(audio(.3), 'lux-id', c.session_id, 'i', original,
                                  source='voiceprint', score=.95) is None
            assert engine.store.samples() == []
            # An explicit new manual click is allowed to re-add it.
            await c.label_personal_inputs(['i'], 'lux-id')
            assert len(engine.store.samples()) == 1
        finally:
            await c.close()
            engine.close()
    asyncio.run(run())


def test_jev_classification_scores_are_not_voiceprint_similarities(process):
    from jshi.voice.jev import VoiceJEV
    requests = []
    batch = [{'n': 1, 'who': 'P1', 'text': '你好', 'basis': 'personal_classifier',
              'voice_evidence': {'pick': 'P1', 'strength': 'clear', 'basis': '个人分类'},
              'candidates': [{'who': 'P1', 'score': .982345, 'source': 'personal_classifier'},
                             {'who': 'P2', 'score': .55, 'source': 'voiceprint'}]}]
    VoiceJEV(process.cognition, mode='legacy').decide_batch('stone', batch, [], {}, on_request=requests.append)
    payload = json.loads(requests[0].input_text)
    line = payload['input']['lines'][0]
    first, second = line['candidates']
    assert first['classification_score'] == .982 and 'similarity' not in first
    assert second['similarity'] == .55
    assert line['voice_ranking']['first'] == 'P2'


def test_short_input_cannot_use_a_model_validated_only_on_long_audio(process, tmp_path):
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    try:
        engine.select_targets(['lux-id'])
        publish(engine, 'lux-id', tmp_path, 6)
        manifest = engine.manifest('lux-id')
        manifest['active']['duration_reports']['under_1s']['enabled'] = False
        from jshi.voice.personal_model import atomic_json
        atomic_json(engine._directory('lux-id')/'versions.json', manifest)
        short = engine.predict(audio(.3), speakers())
        assert short['ranking'][0]['score'] > .9
        assert not short['matched'] and not short['ranking'][0]['verified']
        assert engine.predict(audio(3), speakers())['matched'] == 'lux-id'
    finally:
        engine.close()


def test_mlp_scores_below_092_are_visible_but_do_not_confirm_identity(process,tmp_path):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    try:
        engine.select_targets(['lux-id'])
        publish(engine,'lux-id',tmp_path,2)
        result=engine.predict(audio(),speakers())
        assert .8 < result['ranking'][0]['score'] < .92
        assert result['ranking'][0]['threshold']==.92
        assert not result['matched']
    finally:engine.close()


def test_pending_training_cannot_overwrite_explicit_rollback_or_pause(process, tmp_path):
    engine = PersonalSpeakers(tmp_path/'personal', process.profiles)
    try:
        engine.select_targets(['lux-id'])
        first = publish(engine, 'lux-id', tmp_path, 6)
        second = publish(engine, 'lux-id', tmp_path, 5)
        candidate = tmp_path/'pending.npz'
        np.savez(candidate, **weights(4))
        revision, epoch = engine.store.revision()
        snapshot = {'object_id': 'lux-id', 'model_id': 'fixture-encoder', 'dimension': 2,
                    'revision': revision, 'label_epoch': epoch, 'base_version': second['version']}
        result = {key: second[key] for key in ('threshold', 'validation', 'calibration', 'counts', 'duration_reports')}
        engine.rollback('lux-id')
        with pytest.raises(ValueError, match='已经切换或回退'):
            engine.publish('lux-id', snapshot, candidate, result)
        assert engine.manifest('lux-id')['active']['version'] == first['version']
        snapshot['base_version'] = first['version']
        engine.store.configure({'enabled': False})
        with pytest.raises(ValueError, match='暂停'):
            engine.publish('lux-id', snapshot, candidate, result)
    finally:
        engine.close()
