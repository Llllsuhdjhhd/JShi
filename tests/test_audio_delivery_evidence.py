"""Delayed original audio stays historical evidence across model boundaries."""
import json

import pytest

from jshi.voice.scene_material import audio_delivery_evidence, dialogue_row
from jshi.voice.jev import VoiceJEV
from tests.test_jev_split_modes import Model, entry, main_preparation


@pytest.mark.parametrize('timing', [
    {}, {'_audio_end_received_mono': 10},
    {'_audio_end_received_mono': 10, '_asr_return_mono': 9},
    {'_audio_end_received_mono': float('nan'), '_asr_return_mono': 11},
    {'_audio_end_received_mono': True, '_asr_return_mono': 11},
])
def test_missing_or_invalid_receipt_never_falls_back_to_sample_clock(timing):
    assert audio_delivery_evidence({'asr_timing': timing,
        'recorded_end_at_ms': 1, 'received_at_ms': 999999}) == {}


def test_delay_uses_arrival_interval_without_raw_clocks_or_precision():
    assert audio_delivery_evidence({'asr_timing': {
        '_audio_end_received_mono': 10.13, '_asr_return_mono': 128.66,
        '_result_ready_mono': 500}}) == {'recognition_delay_seconds': 119}


def test_actual_entry_and_main_support_keep_late_audio_without_extra_calls():
    model = Model([entry(), main_preparation()])
    delay = {'recognition_delay_seconds': 119}
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '匠石，帮我看看。',
                   'audio_delivery': delay}], [], {'state': 'completed'})
    assert result.route == 'main' and len(model.requests) == 2
    assert '"recognition_delay_seconds":119' in model.requests[0].input_text
    assert '"recognition_delay_seconds":119' in model.requests[1].input_text
    assert '119' in json.dumps(result.entry_support, ensure_ascii=False)
    assert dialogue_row({'text': '原话', 'audio_delivery': delay})['audio_delivery'] == delay
