"""Unit tests for VoiceConversation.unfinished mid-turn cues."""
import pytest

from jshi.app.voice import VoiceConversation


@pytest.mark.parametrize(
    'text',
    [
        '在忙什么',
        '我想问一件事',
        '你听得到吗',
        '主使',
        '就是这个',
        '给我一些',
        '还有',
        '好的',
        '所以',
        '变成',
        '那个',
        '这个',
        '一些',
    ],
)
def test_unfinished_false_for_complete_short_replies(text):
    assert VoiceConversation.unfinished(text) is False


@pytest.mark.parametrize(
    'text',
    [
        '我想',
        '比如说',
        '变成了那种…',
        '那我回来',
        '变成了',
        '我觉得',
        '的那种感觉',
    ],
)
def test_unfinished_true_for_half_sentences(text):
    assert VoiceConversation.unfinished(text) is True
