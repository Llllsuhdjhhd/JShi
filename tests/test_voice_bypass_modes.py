import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.config import VoiceConfig
from jshi.voice.contracts import SpeechSegment, TimedWord, Transcript
from jshi.voice.diagnostics import diagnostic_log, watch_event_loop
from jshi.voice.transcription_recovery import RecoveryIntake


def test_boundary_recovery_is_active_and_utterance_material_off_by_default(monkeypatch):
    config = VoiceConfig(api_key='k')
    assert config.continuous_analysis == 'active'
    assert config.utterance_material is False
    monkeypatch.setenv('JSHI_VOICE_API_KEY', 'k')
    monkeypatch.delenv('JSHI_VOICE_CONTINUOUS_ANALYSIS', raising=False)
    assert VoiceConfig.from_env().continuous_analysis == 'active'
    monkeypatch.setenv('JSHI_VOICE_API_KEY', 'k')
    monkeypatch.setenv('JSHI_VOICE_CONTINUOUS_ANALYSIS', 'Shadow')
    monkeypatch.setenv('JSHI_VOICE_UTTERANCE_MATERIAL', 'on')
    config = VoiceConfig.from_env()
    assert config.continuous_analysis == 'shadow'
    assert config.utterance_material is True
    with pytest.raises(ValueError):
        VoiceConfig(api_key='k', continuous_analysis='always')


def test_observation_events_go_to_diagnostic_file_not_history(tmp_path):
    stored = []
    repository = SimpleNamespace(path=tmp_path/'subject.sqlite3', add_history=stored.append)
    conversation = SimpleNamespace(process=SimpleNamespace(repository=repository),
                                   subject_id='stone', session_id='s1')
    VoiceConversation.record(conversation, 'audio_timeline_window', {'start_ms': 0, 'end_ms': 8000})
    VoiceConversation.record(conversation, 'cloud_speaker_alignment_failed', {'error_type': 'X'})
    diagnostic_log(tmp_path/'voice_diagnostics.jsonl').flush()
    lines = [json.loads(l) for l in (tmp_path/'voice_diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [l['event_type'] for l in lines] == ['audio_timeline_window']
    assert lines[0]['session_id'] == 's1' and lines[0]['end_ms'] == 8000
    assert [r.event_type for r in stored] == ['cloud_speaker_alignment_failed']


def test_boundary_uses_main_stream_partial_words_before_extra_recognition():
    async def run():
        audio = SessionAudio()
        audio.append(b'\0\0'*80000)
        received, reports, done = [], [], asyncio.Event()
        async def recognize(pcm, offset):
            raise AssertionError('partial words already cover A')
        async def deliver(rows, **kw):
            received.extend(rows)
            done.set()
        intake = RecoveryIntake(audio, recognize, deliver, lambda *a: reports.append(a))
        try:
            # One running partial still contains A and the start of B.
            intake.note_interim(Transcript('请回答我然后', 'p', 0, 2600, False, words=(
                TimedWord('请', 100, 300), TimedWord('回答', 300, 700), TimedWord('我', 700, 950),
                TimedWord('然后', 1200, 2600))))
            await intake.offer((SpeechSegment([], 'acoustic-1', 0, 1000, speaker_cluster_id='acoustic-1'),
                                SpeechSegment([], 'acoustic-2', 1000, 3000, speaker_cluster_id='acoustic-2')), 0, 3000)
            await asyncio.wait_for(done.wait(), 1)
            assert received[0].text == '请回答我' and received[0].final
            assert received[0].speaker_cluster_id == 'acoustic-1' and received[0].identity_tentative
            assert any(kind == 'recovery_interim' for kind, *_ in reports)
        finally:
            await intake.close()
    asyncio.run(run())


def test_boundary_falls_back_to_recognition_when_partial_words_are_thin():
    async def run():
        audio = SessionAudio()
        audio.append(b'\0\0'*80000)
        received, reports, done = [], [], asyncio.Event()
        async def recognize(pcm, offset):
            return (Transcript('请回答我', '', 0, 1000, True),)
        async def deliver(rows, **kw):
            received.extend(rows)
            done.set()
        intake = RecoveryIntake(audio, recognize, deliver, lambda *a: reports.append(a))
        try:
            intake.note_interim(Transcript('请', 'p', 0, 2600, False, words=(TimedWord('请', 100, 300),)))
            await intake.offer((SpeechSegment([], 'acoustic-1', 0, 1000, speaker_cluster_id='acoustic-1'),), 0, 3000)
            await asyncio.wait_for(done.wait(), 1)
            assert received[0].text == '请回答我'
            assert any(kind == 'recovery_recognized' for kind, *_ in reports)
        finally:
            await intake.close()
    asyncio.run(run())


@pytest.mark.parametrize('silence_ms,ready', [(300, True), (250, False)])
def test_boundary_needs_300ms_silence_after_speech(silence_ms, ready):
    async def run():
        audio = SessionAudio()
        audio.append(b'\0\0'*80000)
        async def recognize(pcm, offset): return ()
        async def deliver(rows, **kw): pass
        intake = RecoveryIntake(audio, recognize, deliver, lambda *a: None)
        try:
            await intake.offer((SpeechSegment([], 'a', 0, 1000),), 0, 1000+silence_ms)
            assert bool(intake.attempted) is ready
        finally:
            await intake.close()
    asyncio.run(run())


def test_extra_recognitions_are_capped_per_minute():
    async def run():
        audio = SessionAudio()
        audio.append(b'\0\0'*160000)
        calls, reports = [], []
        async def recognize(pcm, offset):
            calls.append(offset)
            return ()
        async def deliver(rows, **kw): pass
        intake = RecoveryIntake(audio, recognize, deliver, lambda *a: reports.append(a[0]))
        intake.max_recognitions_per_minute = 2
        try:
            await intake.offer(tuple(SpeechSegment([], f'a{i}', i*1000, i*1000+600) for i in range(4)), 0, 5000)
            for _ in range(50):
                if not intake.pending:
                    break
                await asyncio.sleep(.01)
            assert calls == [0, 1000]
            assert reports.count('recovery_rate_limited') == 2
        finally:
            await intake.close()
    asyncio.run(run())


def test_event_loop_watch_reports_blocking():
    async def run():
        reports = []
        task = asyncio.create_task(watch_event_loop(lambda *a: reports.append(a),
                                                    interval_s=.02, window_s=.1, threshold_ms=50))
        await asyncio.sleep(.03)
        time.sleep(.12)
        await asyncio.sleep(.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert reports and reports[0][0] == 'event_loop_lag' and reports[0][1]['max_ms'] >= 50
    asyncio.run(run())
