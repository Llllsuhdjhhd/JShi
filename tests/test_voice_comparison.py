from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from jshi.voice.comparison import SpeakerTrial
from jshi.voice.volc import Transcript


def row(value, seconds, start=0):
    return {'pcm': np.full(int(seconds*16000), value, dtype='<i2').tobytes(),
        'transcript': Transcript('样本', 'A', start, start+int(seconds*1000), True)}


def model():
    def embed(a):
        if len(a) < 24000: return None
        return [1., 0.] if a[0]>0 else [0., 1.]
    return SimpleNamespace(embedding=embed, lock=RLock(), model_id='test', threshold=.65, margin=.08)


def test_held_out_trial_counts_misses_and_unknown_false_accepts(tmp_path):
    trial=SpeakerTrial(tmp_path, {'first':model, 'second':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    trial.add('lux','test',[row(1,2)],'two')
    trial.add('lux','test',[row(2,.5)],'three')
    trial.add('other','test',[row(-1,2)],'four')
    report=trial.run()
    assert report['cloud']['status']=='not_run'
    for result in report['models'].values():
        assert result['enrollment_extracted']==1
        assert result['known_tests']==2 and result['misses']==1
        assert result['unknown_tests']==1 and result['false_accepts']==0
        assert result['results'][0]['predicted']=='lux'
    restored=SpeakerTrial(tmp_path,{'first':model})
    assert len(restored.samples)==4
    assert (tmp_path/'report.json').is_file()


def test_same_or_overlapping_audio_cannot_be_both_enrollment_and_test(tmp_path):
    trial=SpeakerTrial(tmp_path, {'first':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    with pytest.raises(ValueError,match='已加入'):
        trial.add('lux','test',[row(1,5)],'two')
    with pytest.raises(ValueError,match='重叠'):
        trial.add('lux','test',[row(2,2,1000)],'one')
    assert len(trial.samples)==1


def test_model_failure_is_reported_without_invalidating_other_model(tmp_path):
    trial=SpeakerTrial(tmp_path, {'missing':lambda:None,'working':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    trial.add('lux','test',[row(1,2)],'two')
    report=trial.run()
    assert report['models']['missing']['status']=='unavailable'
    assert report['models']['working']['status']=='ok'


def test_invalid_sample_ids_cannot_escape_trial_directory(tmp_path):
    trial=SpeakerTrial(tmp_path,{'first':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    trial.add('lux','test',[row(1,2)],'two')
    trial.samples[0]['id']='../outside'
    with pytest.raises(ValueError,match='无效'):
        trial.run()


def test_new_group_preserves_old_labels_and_audio(tmp_path):
    trial=SpeakerTrial(tmp_path,{'first':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    iid=trial.samples[0]['id']
    assert trial.new_group()['samples']==[]
    assert (tmp_path/(iid+'.wav')).is_file()
    assert len(list(tmp_path.glob('group-*.json')))==1
    assert SpeakerTrial(tmp_path,{'first':model}).samples==[]


def test_calibration_reports_score_distributions_and_suggested_thresholds(tmp_path):
    trial=SpeakerTrial(tmp_path,{'first':model})
    trial.add('lux','enroll',[row(1,5)],'one')
    trial.add('lux','test',[row(1,2)],'two')
    trial.add('other','test',[row(-1,2)],'three')
    c=trial.run()['models']['first']['calibration']
    assert c['status']=='ok' and c['genuine_pairs']==1 and c['impostor_pairs']==1
    assert c['separable'] and c['eer']==0 and not c['reliable']
    assert c['impostor']['max'] < c['strict_threshold'] <= c['genuine']['min']


def test_calibration_with_overlapping_scores_balances_miss_and_false_accept():
    from jshi.voice.comparison import calibrate
    c=calibrate([.3,.5,.6,.7],[.2,.35,.55,.4])
    assert c['status']=='ok' and not c['separable']
    assert c['eer']==.25 and c['strict_threshold']==.56 and c['strict_miss_rate']==.5
    assert calibrate([.9],[])['status']=='insufficient'


def test_human_labeled_comparison_keeps_one_voiceprint_so_the_other_direction_misses(tmp_path):
    trial=SpeakerTrial(tmp_path,{'first':model})
    trial.add('lux','enroll',[row(1,5),row(-1,5,6000)],'one')
    trial.add('lux','test',[row(2,2)],'two')
    trial.add('lux','test',[row(-2,2)],'three')
    result=trial.run()['models']['first']
    assert result['enrollment_accepted']==1 and not result['enrollment_failures']
    assert result['known_tests']==2 and result['misses']==1
