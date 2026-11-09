import asyncio
from types import SimpleNamespace
from dataclasses import replace
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.contracts import Transcript, TimedWord
from jshi.voice.transcription_recovery import RecoveryIntake
from jshi.voice.speaker_evidence import TrackLinker
from jshi.voice.contracts import SpeechSegment


def test_empty_result_can_retry_after_half_second_but_errors_keep_cooldown(monkeypatch):
    import jshi.voice.transcription_recovery as module
    async def run():
        now=[100.0]
        monkeypatch.setattr(module,'monotonic',lambda:now[0])
        async def deliver(*a,**kw):pass
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        # Exercise eligibility without running a cloud job.
        intake.task.cancel()
        try:
            s=SpeechSegment([], 'a',478368,479954)
            intake.failed_attempts[(478321,479992)]=(100,1,.5)
            now[0]=100.49;await intake._offer_uncovered(s,480500)
            assert not intake.pending
            now[0]=100.51;await intake._offer_uncovered(s,480500)
            assert len(intake.pending)==1
            intake.pending.clear();intake.attempted.clear()
            intake.failed_attempts[(478321,479992)]=(100,1,2)
            await intake._offer_uncovered(s,480500)
            assert not intake.pending
            intake.failed_attempts[(478321,479992)]=(100,2,.5)
            now[0]=103;await intake._offer_uncovered(s,480500)
            assert not intake.pending
        finally:await intake.close()
    asyncio.run(run())


def test_preserved_sentence_survives_reconciliation_with_12ms_prefix_difference():
    from jshi.voice.cloud_alignment import align_words
    async def run():
        delivered=[]
        async def deliver(rows,**kw):delivered.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            early=Transcript('陆广今天作业多吗','a',457792,458651,True,speaker_cluster_id='a')
            await intake.publish((early,),preliminary=True,source_span=(457543,458707))
            iid=delivered[0].input_id
            # Archived prefix times; the remaining tokens are grouped for this test.
            final=Transcript(early.text,'0',457351,459002,True,words=(
                TimedWord('陆',457511,457551),TimedWord('广今天作业多吗',457711,458711)))
            aligned=align_words(final,(SpeechSegment([], 'a',457543,458707),))
            assert len(aligned)==1 and aligned[0].identity_uncertain
            await intake.publish(aligned,source=final)
            assert len(delivered)==2 and delivered[-1].text==final.text
            assert delivered[-1].replaces_input_ids==(iid,)
            assert delivered[-1].identity_uncertain and not delivered[-1].voiceprint_id
            await intake.publish(aligned,source=final)
            assert len(delivered)==2
        finally:await intake.close()
    asyncio.run(run())


def test_internal_gaps_update_whole_sentence_instead_of_adding_stray_words():
    from jshi.voice.cloud_alignment import align_words
    async def run():
        delivered=[]
        async def deliver(rows,**kw):delivered.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            # Constructed sentence, recorded pattern of same voice around a gap.
            for text,a,b in [('入冬',0,900),('不至于',1700,2500)]:
                await intake.publish((Transcript(text,'a',a,b,True,speaker_cluster_id='a'),),
                                     preliminary=True,source_span=(a,b))
            ids=tuple(r.input_id for r in delivered)
            final=Transcript('入冬的氛围不至于','0',0,2500,True,words=(
                TimedWord('入冬',0,900),TimedWord('的氛围',950,1600),TimedWord('不至于',1700,2500)))
            aligned=align_words(final,(SpeechSegment([], 'a',0,900),SpeechSegment([], 'a',1700,2500)))
            assert len(aligned)==1
            await intake.publish(aligned,source=final)
            assert len(delivered)==3 and delivered[-1].text==final.text
            assert delivered[-1].replaces_input_ids==ids
            assert delivered[-1].identity_uncertain
        finally:await intake.close()
    asyncio.run(run())


def test_recorded_same_voice_fragments_merge_inside_multispeaker_final():
    from jshi.voice.cloud_alignment import align_words
    async def run():
        delivered=[]
        async def deliver(rows,**kw):delivered.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            for text,a,b,track in [('听到了那就按你说的来累了就先',5332,8332,'known'),
                                  ('歇会',8337,9019,'unidentified-short'),
                                  ('别硬撑',9052,9612,'unidentified-short2'),
                                  ('我们就停在这你休息好了再叫我',10572,13272,'known')]:
                await intake.publish((Transcript(text,track,a,b,True,speaker_cluster_id='acoustic-2'),),
                                     preliminary=True,source_span=(a,b))
            ids=tuple(r.input_id for r in delivered)
            words=(TimedWord('嗯好',4652,5252),TimedWord('听到了那就按你说的来累了就先',5332,8332),
                   TimedWord('歇会',8337,9019),TimedWord('别硬撑',9052,9612),
                   TimedWord('我们就停在这你休息好了再叫我',10572,13272))
            source=Transcript('嗯好，听到了那就按你说的来累了就先歇会，别硬撑。我们就停在这你休息好了再叫我。',
                'provider',3332,13852,True,words=words)
            segments=(SpeechSegment([],'other',4652,5252,speaker_cluster_id='acoustic-1'),
                      SpeechSegment([],'known',5332,9692,speaker_cluster_id='acoustic-2'),
                      SpeechSegment([],'known',10492,13332,speaker_cluster_id='acoustic-2'))
            aligned=align_words(source,segments)
            assert len(aligned)==2
            await intake.publish(aligned,source=source)
            merged=next(r for r in delivered[4:] if r.replaces_input_ids)
            assert merged.replaces_input_ids==ids
            assert '先歇会，别硬撑' in merged.text and '嗯好' not in merged.text
            assert len([r for r in delivered[4:] if not r.replaces_input_ids])==1
            count=len(delivered)
            await intake.publish(aligned,source=source)
            assert len(delivered)==count, [(r.text,r.replaces_input_ids) for r in delivered[count:]]
        finally:await intake.close()
    asyncio.run(run())


def test_empty_six_second_checkpoint_does_not_block_later_short_boundary():
    async def run():
        audio=SessionAudio(); audio.append(b'\0\0'*432000)
        calls,received=[],[]
        empty,done=asyncio.Event(),asyncio.Event()
        async def recognize(pcm,offset):
            calls.append((offset,offset+len(pcm)//32))
            if len(pcm)>2*32000:
                return ()
            return (Transcript('下次你可以用得上。','',offset,offset+len(pcm)//32,True),)
        async def deliver(rows,**kw):received.extend(rows);done.set()
        def report(k,v):
            if k=='recovery_work_timing' and v['stage']=='delivered':empty.set()
        intake=RecoveryIntake(audio,recognize,deliver,report)
        try:
            await intake.offer((SpeechSegment([], 'a',19082,25082),),18000,26000)
            await asyncio.wait_for(empty.wait(),2)
            # Same failed range is cooled down, not retried every rolling tick.
            await intake.offer((SpeechSegment([], 'a',19082,25082),),18000,26000)
            assert not intake.pending
            await intake.offer((SpeechSegment([], 'a',19102,25102),),18000,26000)
            assert not intake.pending  # 20 ms endpoint jitter is not new evidence
            await intake.offer((SpeechSegment([], 'a',19098,20313),),19000,27000)
            await asyncio.wait_for(done.wait(),2)
            assert calls==[(19082,25082),(19098,20313)]
            assert len(received)==1 and received[0].text=='下次你可以用得上。'
        finally:await intake.close()
    asyncio.run(run())


def test_a_reaches_judgment_without_cloud_final_or_b_finishing():
    async def run():
        audio = SessionAudio()
        audio.append(b'\0\0'*160000)
        received, done = [], asyncio.Event()
        async def recognize(pcm, offset):
            assert offset == 0
            return (Transcript('请回答我', '', 0, 1000, True),)
        async def deliver(rows, **kw):
            received.extend(rows)
            done.set()
        recovery = RecoveryIntake(audio,recognize,deliver,lambda *a:None)
        try:
            a = SpeechSegment([], 'acoustic-1', 0,1000, speaker_cluster_id='acoustic-1')
            # B is still speaking; no final full-stream text exists.
            b = SpeechSegment([], 'acoustic-2', 1000,3000, speaker_cluster_id='acoustic-2')
            await recovery.offer((a,b),0,3000)
            await asyncio.wait_for(done.wait(),2)
            assert received[0].text == '请回答我'
            assert received[0].speaker_cluster_id == 'acoustic-1'
            assert not received[0].voiceprint_id
            await recovery.publish((replace(received[0], text='请回答我。'),))
            assert len(received) == 2 and received[-1].replaces == ((0,1000),)
        finally:
            await recovery.close()
    asyncio.run(run())


def test_window_labels_are_linked_by_shared_audio_not_number():
    linker = TrackLinker()
    first = linker.link((SpeechSegment([], 'local0',0,3000),SpeechSegment([], 'local1',3000,6000)))
    second = linker.link((SpeechSegment([], 'local0',3000,6000),SpeechSegment([], 'local1',1000,3000)))
    assert second[0].track_id == first[1].track_id
    assert second[1].track_id == first[0].track_id


def test_long_cloud_packet_keeps_only_uncovered_words_without_bridging_old_audio():
    async def run():
        delivered = []
        async def deliver(rows,**kw):delivered.extend(rows)
        recovery = RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            recovery.covered.append((1000,2000))
            row = Transcript('前中后','x',0,3000,True,words=(
                TimedWord('前',0,1000),TimedWord('中',1000,2000),TimedWord('后',2000,3000)))
            await recovery.publish((row,))
            assert [r.text for r in delivered] == ['前','后']
            assert not recovery.covered_span(5000,5050)
        finally:await recovery.close()
    asyncio.run(run())


def test_missing_word_times_preserves_new_content_as_unresolved_revision_context():
    async def run():
        delivered = []
        async def deliver(rows, **kw): delivered.extend(rows)
        recovery = RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            recovery.covered.append((0,1000))
            await recovery.publish((Transcript('A的话以及B的新话','provider',0,10000,True),))
            assert len(delivered) == 1
            assert delivered[0].audio_kind == 'revision_context'
            assert delivered[0].identity_uncertain
            assert 'B的新话' in delivered[0].text
        finally: await recovery.close()
    asyncio.run(run())


def test_partially_overlapping_checkpoint_does_not_repeat_old_suffix():
    async def run():
        received=[]
        async def deliver(rows,**kw):received.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            intake.covered.append((0,4000))
            await intake.publish((Transcript('旧后缀新内容','A',3000,5000,True,words=(
                TimedWord('旧后缀',3000,4000),TimedWord('新内容',4000,5000))),))
            assert [r.text for r in received] == ['新内容']
        finally:await intake.close()
    asyncio.run(run())


def test_full_stream_replaces_checkpoint_text_despite_different_word_times():
    async def run():
        received=[]
        async def deliver(rows,**kw):received.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            await intake.publish((Transcript('降十','a',3000,3900,True),), preliminary=True)
            await intake.publish((Transcript('匠石，你好。','provider',2950,5000,True,
                words=(TimedWord('匠石',2950,4000),TimedWord('你好',4100,5000))),))
            assert received[-1].text == '匠石，你好。'
            assert received[-1].replaces == ((3000,3900),)
            assert list(intake.covered) == [(2950,5000)]
            assert not intake.preliminary_spans
            await intake.publish((Transcript('降十','a',3100,4000,True),), preliminary=True)
            assert len(received) == 2
        finally:await intake.close()
    asyncio.run(run())


def test_word_slightly_past_early_text_edge_is_not_delivered_again():
    async def run():
        received=[]
        async def deliver(rows,**kw):received.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            await intake.publish((Transcript('是李鹏会去吗','a',140438,141383,True),), preliminary=True)
            # Recognizers place word edges differently; this word is mostly inside the early text.
            await intake.publish((Transcript('吗','provider',141241,141441,True,
                words=(TimedWord('吗',141241,141441),)),))
            assert [r.text for r in received] == ['是李鹏会去吗']
        finally:await intake.close()
    asyncio.run(run())


def test_archived_name_and_tail_do_not_become_three_new_inputs():
    """Exact archived edge times; middle text is a constructed packet, not a raw log."""
    async def run():
        received=[]
        async def deliver(rows,**kw): received.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            early=Transcript('匠石有人说我有点累如果不让你安慰只让你做一个动作你会做啥',
                             'a',574552,579732,True)
            await intake.publish((early,),preliminary=True,source_span=(573914,579955))
            iid=received[0].input_id
            prefix=Transcript('匠石。','provider',573711,574351,True,
                words=(TimedWord('匠',573991,574031),TimedWord('石',574311,574351)))
            split=tuple(replace(prefix,text=w.text,start_ms=w.start_ms,end_ms=w.end_ms,words=(w,)) for w in prefix.words)
            await intake.publish(split,source=prefix)
            assert len(received)==1
            body=Transcript(early.text[2:],'provider',574671,580471,True,
                words=(TimedWord(early.text[2:-1],574671,579591),TimedWord('啥',579591,579911)))
            await intake.publish((body,),source=body)
            assert len(received)==2
            assert received[-1].input_id==iid and received[-1].replaces_input_ids==(iid,)
            assert received[-1].text=='匠石。'+early.text[2:]
            await intake.publish((body,),source=body)
            assert len(received)==2  # retransmission, not another revision or question
        finally: await intake.close()
    asyncio.run(run())


def test_final_a_and_new_b_share_packet_but_not_input_identity():
    async def run():
        received=[]
        async def deliver(rows,**kw):received.extend(rows)
        intake=RecoveryIntake(None,None,deliver,lambda *a:None)
        try:
            await intake.publish((Transcript('开灯','a',100,900,True),),
                                 preliminary=True,source_span=(0,1000))
            iid=received[0].input_id
            source=Transcript('关灯你好','provider',0,2000,True,
                words=(TimedWord('关灯',100,900),TimedWord('你好',1100,1900)))
            await intake.publish((source,),source=source)
            assert [r.text for r in received]==['开灯','关灯','你好']
            assert received[1].replaces_input_ids==(iid,)
            assert received[2].input_id!=iid and not received[2].replaces
            again=Transcript('关灯','a',3000,4000,True,words=(TimedWord('关灯',3100,3900),))
            await intake.publish((again,),source=again)
            assert received[-1].input_id!=iid and not received[-1].replaces
        finally:await intake.close()
    asyncio.run(run())


def test_continuous_18_second_speech_waits_for_boundary_before_recognition_and_jev():
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*304000)
        received=[];calls=[];done=asyncio.Event()
        async def deliver(rows,**kw): received.extend(rows);done.set()
        async def recognize(pcm,start):
            calls.append((start,len(pcm)//32))
            return (Transcript(str(start),'',start,start+len(pcm)//32,True),)
        intake=RecoveryIntake(audio,recognize,deliver,lambda *a:None)
        try:
            for end in (6000,12000,18000):
                start=max(0,end-8000)
                await intake.offer((SpeechSegment([], 'a',start,end),),start,end)
                await asyncio.sleep(0)
                assert not calls and not received and not intake.pending
            # The timeline supplies accumulated boundaries, not just its last 8s.
            await intake.offer((SpeechSegment([], 'a',0,18000),),11000,19000)
            await asyncio.wait_for(done.wait(),2)
            assert calls==[(0,18000)]
            assert [(r.start_ms,r.end_ms) for r in received]==[(0,18000)]
        finally: await intake.close()
    asyncio.run(run())


def test_asr_final_can_deliver_without_waiting_for_a_recovery_boundary():
    async def run():
        delivered=[]
        async def deliver(rows,**kw):delivered.extend(rows)
        async def recognize(*args):raise AssertionError('no extra ASR before a boundary')
        intake=RecoveryIntake(None,recognize,deliver,lambda *a:None)
        try:
            await intake.offer((SpeechSegment([], 'a',0,6600),),0,6600)
            assert not intake.pending and not delivered
            row=Transcript('这段完整文字来自主路。','a',0,6600,True)
            await intake.publish((row,),source=row)
            assert [r.text for r in delivered]==[row.text]
            assert not intake.pending
        finally:await intake.close()
    asyncio.run(run())
