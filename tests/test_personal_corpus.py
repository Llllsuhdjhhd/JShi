from __future__ import annotations

import io
from pathlib import Path
import wave

import numpy as np
import pytest

from jshi.recognition import ObjectProfile
from jshi.voice.personal import PersonalSpeakers
from jshi.voice.personal_corpus import decode_clip, import_parquet, SOURCE
from jshi.voice.personal_model import partition
from jshi.voice.personal_store import PersonalStore
from jshi.voice.personal_train import groups
from tests.test_personal_speakers import audio, speakers
from tests.test_voice import process


def public(store, speaker, clip):
    return store.save_background(audio(.6), dataset='aishell-1', speaker=speaker, clip=clip,
                                 source_url=SOURCE, license='Apache-2.0')


def test_other_people_require_manual_confirmation_for_negatives(process, tmp_path):
    process.profiles.create(ObjectProfile('automatic-other', '暂时自动认出的人', status='confirmed'))
    process.profiles.create(ObjectProfile('manual-other', 'luguang', status='confirmed'))
    service, encoder = PersonalSpeakers(tmp_path/'personal', process.profiles), speakers()
    try:
        service.store.save(audio(), 'lux-id', 'own', 'p', source='manual')
        service.store.save(audio(), 'automatic-other', 'others', 'a', source='voiceprint', score=.9)
        service.store.save(audio(), 'manual-other', 'others', 'm', source='manual')
        sid = public(service.store, 'S0764', 'BAC009S0764W0121.wav')
        snapshot = service._records('lux-id', encoder)
        assert len(snapshot['records']) == 3
        assert sum(r['label'] == 1 for r in snapshot['records']) == 1
        assert {r['source'] for r in snapshot['records'] if not r['label']} == {'manual', 'public'}
        assert service.store.features(sid, encoder.model_id, background=True)
        own_other = service._records('automatic-other', encoder)
        assert any(r['source'] == 'voiceprint' and r['label'] == 1 for r in own_other['records'])
        progress = service.dataset_progress('lux-id')
        assert (progress['positive'], progress['negative_manual'], progress['negative_public']) == (1, 1, 1)
        assert progress['minimum']['train'] == {'positive': 12, 'negative': 24}
        service.store.save(audio(), 'automatic-other', 'others', 'a', source='manual')
        assert service.dataset_progress('lux-id')['negative_manual'] == 2
        assert len(process.profiles.list()) == 3  # No fabricated local people from public speakers.
    finally:
        service.close()


def test_public_speakers_never_cross_training_and_holdout_groups(tmp_path):
    store = PersonalStore(tmp_path)
    records = []
    for name in ('train', 'calibration', 'validation'):
        speaker = next(f'S{i:04d}' for i in range(1000)
                       if partition('public:aishell-1:'+f'S{i:04d}') == name)
        for i in range(2):
            public(store, speaker, f'{speaker}-{i}')
    for row in store.background_samples():
        records.append({**row, 'label': 0, 'source': 'public'})
    splits = groups(records)
    assert all(len(splits[name]) == 2 for name in splits)
    sets = [{r['speaker'] for r in rows} for rows in splits.values()]
    assert all(not a & b for i, a in enumerate(sets) for b in sets[i+1:])
    rev = store.revision()
    first = store.background_samples()[0]
    public(store, first['speaker'], first['clip'])
    assert store.revision() == rev
    with pytest.raises(ValueError, match='不一致'):
        store.save_background(audio(.6, -1000), dataset='aishell-1', speaker=first['speaker'],
            clip=first['clip'], source_url=SOURCE, license='Apache-2.0')


def wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
        wav.writeframes(audio(4.5))
    return buf.getvalue()


def test_small_corpus_import_preserves_speaker_ids_and_original_clip_count(tmp_path):
    pa = pytest.importorskip('pyarrow')
    pq = pytest.importorskip('pyarrow.parquet')
    rows = [{'audio': {'path': f'BAC009S{speaker:04d}W{i:04d}.wav', 'bytes': wav_bytes()}}
            for speaker in (1, 2) for i in range(60)]
    path = tmp_path/'corpus.parquet'
    pq.write_table(pa.Table.from_pylist(rows), path)
    store = PersonalStore(tmp_path/'personal')
    report = import_parquet([path], store, per_speaker=6)
    assert report['clips'] == 12 and report['speakers'] == 2
    assert import_parquet([path], store, per_speaker=6)['clips'] == 12
    metadata = store.background_samples()
    assert all(r['license'] == 'Apache-2.0' and r['source_url'] == SOURCE for r in metadata)
    assert sorted(round(r['seconds'], 1) for r in metadata) == [.6]*4 + [1.5]*4 + [4.]*4
    assert not store.samples()
    for row in metadata:
        assert row['session_id'].endswith(row['speaker'])
    assert len(decode_clip(wav_bytes(), 'BAC009S0001W0001.wav')) in (19200, 48000, 128000)
    # New files must broaden speakers, not reset quotas for previously saved people.
    extra = [{'audio': {'path': f'BAC009S{speaker:04d}W{i:04d}.wav', 'bytes': wav_bytes()}}
             for speaker in (1, 2, 3) for i in range(1000, 1060)]
    extra_path = tmp_path/'additional.parquet'
    pq.write_table(pa.Table.from_pylist(extra), extra_path)
    expanded = import_parquet([extra_path], store, per_speaker=6)
    assert expanded['clips'] == 18 and expanded['speakers'] == 3
    assert expanded['processed'] == 6
    assert import_parquet([path, extra_path], store, per_speaker=6)['processed'] == 0
    # Raising the quota must add genuinely new clips, without counting old ones again.
    larger = import_parquet([path, extra_path], store, per_speaker=9)
    assert larger['processed'] == 9 and larger['clips'] == 27
    assert import_parquet([path, extra_path], store, per_speaker=9)['processed'] == 0
