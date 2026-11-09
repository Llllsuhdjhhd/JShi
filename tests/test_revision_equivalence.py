import pytest
from jshi.voice.transcripts import equivalent_input_revision

@pytest.mark.parametrize('old,new', [('匠石，有人说你笨。','匠石有人说你笨'),('你好，世界。','你好世界'),('ＡＢＣ。','ABC')])
def test_format_only_revisions(old,new):assert equivalent_input_revision(old,new)

@pytest.mark.parametrize('old,new', [('不要，停','不要停'),('1.5','15'),('可以？','可以。'),('陆广来了','陆光来了'),('开灯','关灯'),('我有三本书','我有四本书'),('我想去','我不去')])
def test_small_edits_are_not_automatically_meaningless(old,new):assert not equivalent_input_revision(old,new)


@pytest.mark.parametrize('old,new', [
    ('匠石有人说我有点累如果不让你安慰只让你做一个动作你会做啥',
     '匠石，有人说我有点累，如果不让你安慰，只让你做一个动作，你会做啥？'),
    ('陆广今天作业多吗','陆广，今天作业多吗？')])
def test_final_punctuation_of_lexical_question_is_equivalent(old,new):
    assert equivalent_input_revision(old,new)
    assert equivalent_input_revision(new,old)


@pytest.mark.parametrize('old,new',[('我买了几本书','我买了几本书？'),('1,234','1234'),('不，要','不要'),('我没有，吃饭','我没有吃饭')])
def test_semantically_sensitive_punctuation_is_not_discarded(old,new):
    assert not equivalent_input_revision(old,new)
