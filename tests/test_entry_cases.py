import asyncio
from dataclasses import dataclass
import io
import json
import wave

import pytest

from jshi.testing.entry_cases.store import CaseStore
from jshi.testing.entry_cases.runner import replay


def wav(samples=3200):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\0\0' * samples)
    return out.getvalue()


def test_media_survives_source_and_case_versions(tmp_path):
    store = CaseStore(tmp_path/'library')
    source = tmp_path/'source.wav'; source.write_bytes(wav())
    ident = store.import_wav(source.read_bytes())
    source.unlink()
    case = store.save({'schema_version': 1, 'name': '停顿', 'timeline': [{'type': 'audio', 'at_ms': 0, 'media': ident}]})
    second = store.save({**case, 'name': '续句'})
    assert store.load(case['id'])['name'] == '续句'
    assert store.path('scenarios', case['id'], case['version']+'.json').exists()
    assert second['version'] != case['version']
    assert store.path('media', ident, 'original.wav').read_bytes() == wav()
    with pytest.raises(ValueError): store.path('media', '..', 'secret')
    with pytest.raises(ValueError): store.import_wav(wav(), receipts=[{'start_sample': 0, 'samples': 1, 'received_ms': 0}])


def test_audio_is_streamed_and_final_flush_is_explicit(tmp_path):
    store = CaseStore(tmp_path/'library')
    ident = store.import_wav(wav(), receipts=[{'start_sample': 0, 'samples': 1600, 'received_ms': 0},
        {'start_sample': 1600, 'samples': 1600, 'received_ms': 20}])
    fed = []
    class Decoder:
        last_timings = []
        def feed(self, pcm, final=False):
            fed.append((len(pcm), final)); return ()
    case = {'schema_version': 1, 'name': '原声', 'timeline': [{'type': 'audio', 'at_ms': 0, 'media': ident}]}
    result = asyncio.run(replay(store, case, tmp_path/'runs', decoder_factory=lambda _: Decoder(), observation_ms=0))
    assert sum(n for n, _ in fed) == 6400
    assert fed[-1] == (0, True)
    events = json.loads((result/'events.json').read_text())
    assert len([e for e in events if e['type'] == 'audio_received']) == 2
    assert any(e['type'] == 'eof_flush' for e in events)


def test_jev_annotations_never_sent_and_inputs_not_mutated(tmp_path):
    store = CaseStore(tmp_path/'library')
    seen = []
    class Gate:
        def decide_batch(self, subject, batch, context, delivery, **kwargs):
            seen.append((batch.copy(), context, delivery))
            batch.clear()
            return {'route': 'wait'}
    case = {'schema_version': 1, 'name': '冻结', 'annotations': {'secret': '正确答案'},
        'timeline': [{'type': 'jev', 'at_ms': 0, 'batch': [{'n': 1, 'text': '等一下'}]}]}
    run = asyncio.run(replay(store, case, tmp_path/'runs', gate_factory=lambda *_: Gate(), observation_ms=0))
    assert len(seen) == 3 and len(case['timeline'][0]['batch']) == 1
    assert '正确答案' not in str(seen)
    assert json.loads((run/'results.json').read_text())['assessment'] == 'unscored'


def test_missing_runtime_is_not_reported_as_success(tmp_path):
    store = CaseStore(tmp_path/'library')
    case = {'schema_version': 1, 'name': '冻结', 'timeline': [{'type': 'jev', 'at_ms': 0, 'batch': [{'n': 1}]}]}
    with pytest.raises(ValueError, match='gate_factory'):
        asyncio.run(replay(store, case, tmp_path/'runs'))


def test_model_loading_does_not_consume_playback_schedule(tmp_path):
    import time
    store = CaseStore(tmp_path/'library')
    ident = store.import_wav(wav(160))
    class Decoder:
        last_timings = []
        def feed(self, pcm, final=False): return ()
    def build(_):
        time.sleep(.12)
        return Decoder()
    case = {'schema_version': 1, 'name': '加载与输入时钟',
            'timeline': [{'type': 'audio', 'at_ms': 0, 'media': ident}]}
    run = asyncio.run(replay(store, case, tmp_path/'runs', decoder_factory=build, observation_ms=0))
    events = json.loads((run/'events.json').read_text(encoding='utf-8'))
    first = next(e for e in events if e['type'] == 'audio_received')
    assert first['elapsed_ms'] < 100
