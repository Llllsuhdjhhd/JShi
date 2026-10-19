"""One-to-many audio preparation. Original timestamps and mixed audio survive."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


@dataclass(frozen=True)
class SpeakerSource:
    track: str
    pcm: bytes  # mono 16kHz signed int16, aligned with the input window


def original_sources(batch, audio):
    output = []
    for t in batch:
        mixed = t.overlap or any(t.track_id != other.track_id and t.start_ms < other.end_ms
            and other.start_ms < t.end_ms for other in batch)
        if mixed:
            t = replace(t, overlap=True, voiceprint_id='', confidence=None,
                speaker_cluster_id='', identity_uncertain=True, identity_tentative=False,
                identity_note='原始录音含重叠人声，仅供混合声音参考', audio_kind='mixed')
        output.append((t, audio.segment(t.start_ms, t.end_ms), 'mixed' if mixed else 'original'))
    return output


class PyannoteSeparator:
    """Lazy optional model: never download gated weights during normal startup."""
    def __init__(self, model='pyannote/speech-separation-ami-1.0', device='cpu'):
        self.model, self.device, self.pipeline = model, device, None

    def separate(self, pcm):
        import numpy as np
        import torch
        if self.pipeline is None:
            import os
            from pyannote.audio import Pipeline
            self.pipeline = Pipeline.from_pretrained(self.model, use_auth_token=os.getenv('HF_TOKEN'))
            self.pipeline.to(torch.device(self.device))
        waveform = torch.from_numpy(np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768)[None]
        diarization, sources = self.pipeline({'waveform': waveform, 'sample_rate': 16000})
        values = np.asarray(sources.data)
        if values.ndim != 2 or values.shape[0] != len(pcm)//2:
            raise ValueError('分离模型输出与原音频时间轴不一致')
        result = []
        for i, speaker in enumerate(diarization.labels()):
            audio = values[:, i]
            if not np.isfinite(audio).all():
                raise ValueError('分离音轨包含无效数值')
            result.append(SpeakerSource(str(speaker),
                (np.clip(audio, -1, 1)*32767).astype('<i2').tobytes()))
        if not result:
            raise ValueError('分离模型没有返回说话人音轨')
        return result


class SpeakerAudioPreparer:
    """Every batch is a list of speakers; solo speech uses the original PCM.

    Separation is transactional: all sources are transcribed or the original
    inputs remain. No source receives a copy of a mixed transcript.
    """
    def __init__(self, separator=None, transcribe=None, *, timeout_s=30):
        self.separator, self.transcribe, self.timeout_s = separator, transcribe, timeout_s
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-separation')
        self.pending = None

    async def prepare(self, batch, audio):
        mixed = any(t.overlap for t in batch) or any(
            a.track_id != b.track_id and a.start_ms < b.end_ms and b.start_ms < a.end_ms
            for i, a in enumerate(batch) for b in batch[i+1:])
        if not mixed:
            return original_sources(batch, audio)
        if not self.separator:
            return original_sources(batch, audio)
        if not self.transcribe:
            raise ValueError('分离音轨缺少独立转写接口，保留原始混合输入')
        start, end = min(t.start_ms for t in batch), max(t.end_ms for t in batch)
        if end-start > 30000:
            raise ValueError('本批分离窗口超过30秒，保留原始输入')
        pcm = audio.segment(start, end)
        if not pcm:
            raise ValueError('原始音频已不在缓存中，无法分离')
        if self.pending is not None and not self.pending.done():
            raise RuntimeError('上次声音分离仍在计算，本批保留原始输入')
        loop = asyncio.get_running_loop()
        self.pending = loop.run_in_executor(self.executor, self.separator.separate, pcm)
        self.pending.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        async def run():
            sources = await asyncio.shield(self.pending)
            if len({s.track for s in sources}) != len(sources):
                raise ValueError('分离模型返回重复的说话人编号')
            output, scope = [], uuid4().hex[:12]
            # Bound ASR concurrency and preserve all N sources, including N=1.
            for source in sources:
                if len(source.pcm) != len(pcm):
                    raise ValueError('分离音轨长度不一致')
                utterances = await self.transcribe(source.pcm)
                decoded = 0
                for u in utterances:
                    if not u.final or not u.text.strip():
                        continue
                    if not 0 <= u.start_ms < u.end_ms <= end-start:
                        raise ValueError('分离音轨转写时间超出原始窗口')
                    t = replace(u, track_id=f'source-{scope}-{source.track}',
                        start_ms=start+u.start_ms, end_ms=start+u.end_ms,
                        input_id='', voiceprint_id='', confidence=None, overlap=True,
                        audio_kind='separated',
                        speaker_cluster_id='', identity_uncertain=False,
                        identity_tentative=False, replaces=())
                    output.append((t, source.pcm[u.start_ms*32:u.end_ms*32], 'separated'))
                    decoded += 1
                if not decoded:
                    raise ValueError('有分离音轨未取得有效转写，保留原始输入')
            if not output:
                raise ValueError('分离后无有效文字，保留原始输入')
            if len(output) > 128 or sum(len(pcm) for _, pcm, _ in output) > 16*1024*1024:
                raise ValueError('分离结果超过当前音频缓存容量，保留原始输入')
            return sorted(output, key=lambda r: (r[0].start_ms, r[0].end_ms, r[0].track_id))
        return await asyncio.wait_for(run(), self.timeout_s)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
