"""Align timed cloud words to audio-only speaker segments, independent of provider."""
from dataclasses import replace
import json


def unknown(row, reason):
    return replace(row, track_id=f'unidentified-{row.start_ms}', voiceprint_id='', confidence=None,
                   speaker_cluster_id='', identity_uncertain=True, identity_tentative=False,
                   identity_note=json.dumps({'reason': reason, 'person_scope': 'unaligned_cloud_span'}, ensure_ascii=False))


def _attributed(row, segment, text=None, start=None, end=None, words=None):
    return replace(row, text=row.text if text is None else text,
                   start_ms=row.start_ms if start is None else start,
                   end_ms=row.end_ms if end is None else end,
                   words=row.words if words is None else tuple(words),
                   track_id=segment.track_id, overlap=segment.overlap,
                   voiceprint_id=segment.voiceprint_id, confidence=segment.confidence,
                   speaker_cluster_id=segment.speaker_cluster_id,
                   identity_tentative=segment.identity_tentative,
                   identity_uncertain=segment.overlap, identity_note=segment.identity_note)


def align_words(row, segments):
    segments = tuple(segments)
    if not segments:
        return (unknown(row, '没有有效声音分段，不按整段确认单一人物'),)
    if len(segments) == 1 and not segments[0].overlap:
        segment = segments[0]
        covered = max(0, min(segment.end_ms, row.end_ms) - max(segment.start_ms, row.start_ms))
        if covered >= .8 * (row.end_ms - row.start_ms):
            return (_attributed(row, segment),)
    if not row.words:
        return (unknown(row, '存在多个声音段但缺少可靠字词时间戳，不能将整段归给一个人'),)
    # Locate provider tokens in the actual transcript, preserving all text and spaces.
    positioned = []
    cursor = 0
    for word in row.words:
        token = word.text.strip()
        index = row.text.find(token, cursor) if token else -1
        if index < 0 or row.text[cursor:index].strip():
            return (unknown(row, '字词与正文无法可靠对齐，不按整段确认人物'),)
        end = index + len(token)
        positioned.append((word, row.text[cursor:end]))
        cursor = end
    if row.text[cursor:].strip():
        return (unknown(row, '字词没有覆盖完整原话，不按整段确认人物'),)
    groups = []
    for word, text in positioned:
        overlaps = [(max(0, min(word.end_ms, s.end_ms) - max(word.start_ms, s.start_ms)), i)
                    for i, s in enumerate(segments)]
        overlaps.sort(reverse=True)
        duration = word.end_ms - word.start_ms
        best, choice = overlaps[0]
        if (best < .65 * duration or segments[choice].overlap
                or len(overlaps) > 1 and overlaps[1][0] > .2 * duration):
            choice = None
        if groups and groups[-1][0] == choice:
            groups[-1][1].append(word); groups[-1][2].append(text)
        else:
            groups.append((choice, [word], [text]))
    result = []
    for choice, words, texts in groups:
        text = ''.join(texts)
        base = replace(row, text=text, start_ms=words[0].start_ms, end_ms=words[-1].end_ms, words=tuple(words))
        result.append(unknown(base, '换人边界或重叠字词归属不明') if choice is None else _attributed(base, segments[choice]))
    if result:
        result[-1] = replace(result[-1], text=result[-1].text + row.text[cursor:])
    return tuple(result)


class CloudSpeakerAlignment:
    def __init__(self, audio, analyzer):
        self.audio, self.analyzer = audio, analyzer
        self.children = {}

    def process(self, row):
        if not row.final:
            return (row,)
        if row.replaces:
            ranges = tuple(child for source in row.replaces for child in self.children.get(source, (source,)))
            return (unknown(replace(row, replaces=ranges), '云端整体修订，沿用原分段归属或保留跨人修订缺口'),)
        pcm = self.audio.segment(row.start_ms, row.end_ms)
        if pcm is None:
            return (unknown(row, '原音频未完整保留，不能确认整段为同一人'),)
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
        return align_words(row, self.analyzer.analyze(samples, row.start_ms))

    def remember(self, row, result):
        # Record only parts actually delivered, not late results after timeout.
        self.children[(row.start_ms, row.end_ms)] = tuple((r.start_ms, r.end_ms) for r in result)
        while len(self.children) > 128:
            self.children.pop(next(iter(self.children)))
