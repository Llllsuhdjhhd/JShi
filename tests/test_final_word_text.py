from jshi.voice.transcripts import TranscriptAssembler


def payload(**changes):
    row=dict(start_time=8362,end_time=14172,definite=True,
        additions={'speaker_id':'0'},words=[
            {'start_time':8722,'end_time':8802,'text':'按照'},
            {'start_time':8962,'end_time':9042,'text':'这个'},
            {'start_time':9922,'end_time':10002,'text':'呃'}])
    row.update(changes)
    return {'result':{'utterances':[row]}}


def test_final_words_survive_missing_null_or_empty_sentence_text_once():
    for value in ({},{'text':None},{'text':''}):
        assembler=TranscriptAssembler()
        rows=assembler.accept(payload(**value))
        assert len(rows)==1 and rows[0].text=='按照这个呃'
        assert (rows[0].start_ms,rows[0].end_ms,rows[0].track_id)==(8362,14172,'0')
        assert rows[0].final and not rows[0].replaces
        assert assembler.accept(payload(**value))==()


def test_preview_and_invalid_words_never_create_a_final():
    assert not TranscriptAssembler().accept(payload(definite=False))
    for words in ([],[{'text':'虚构'}],[{'start_time':0,'end_time':10,'text':'越界'}]):
        assert not TranscriptAssembler().accept(payload(words=words))
    assert TranscriptAssembler().accept(payload(text='原句。'))[0].text=='原句。'


def test_english_word_boundaries_are_preserved():
    p=payload(words=[{'start_time':8400,'end_time':8500,'text':'hello'},
                     {'start_time':8500,'end_time':8600,'text':'world'}])
    assert TranscriptAssembler().accept(p)[0].text=='hello world'
