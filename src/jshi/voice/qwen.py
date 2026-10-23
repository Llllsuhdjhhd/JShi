"""WSL Qwen3-ASR adapter; audio only, with identity kept on Windows.

Implements the provider-independent Transcriber interface.
Calls run in the existing audio worker, never the browser's event loop.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
from threading import Lock, local
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import wave

from .asr_text import clean_asr_text
from .contracts import Transcript


class QwenRecognizer:
    def __init__(self, config):
        self.url = config.qwen_asr_url.rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('JSHI_QWEN_ASR_URL须为HTTP服务地址，如 http://127.0.0.1:8000/v1')
        self.model = config.qwen_asr_model
        self.timeout = config.qwen_asr_timeout_s
        self.batch_size = config.qwen_asr_batch_size
        self.batch_supported = False
        self.request_lock = Lock()
        self.timing_local = local()

    @property
    def last_timing(self):
        return getattr(self.timing_local, 'value', {})

    @last_timing.setter
    def last_timing(self, value):
        self.timing_local.value = value

    def _request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = Request(self.url + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f'Qwen ASR HTTP {exc.code}，请检查服务及模型名称') from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError('Qwen ASR连接失败或超时，请启动WSL服务并检查JSHI_QWEN_ASR_URL') from exc

    def check(self):
        """Resolve model and batch capabilities before accepting session audio."""
        models = self._request('/models').get('data', [])
        names = [row['id'] for row in models if isinstance(row, dict) and row.get('id')]
        if self.model:
            if self.model not in names:
                raise ValueError('JSHI_QWEN_ASR_MODEL不在WSL服务的模型列表中')
        elif len(names) == 1:
            self.model = names[0]
        else:
            raise ValueError('Qwen服务模型列表不唯一或为空，请设置JSHI_QWEN_ASR_MODEL')
        metadata = next(row for row in models if isinstance(row, dict) and row.get('id') == self.model)
        capability = metadata.get('capabilities', {})
        self.batch_supported = isinstance(capability, dict) and capability.get('audio_batch') is True
        if not self.batch_supported:
            raise ValueError('当前Qwen服务不支持批转写，请使用 tools/start_qwen_batch_wsl.sh 启动批量服务')
        limit = capability.get('max_batch_size')
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError('Qwen服务返回的batch大小无效')
        self.batch_size = min(self.batch_size, limit)


    def transcribe_pcm_many(self, pcms):
        from time import monotonic
        from .latency import elapsed_ms
        self.last_timing = {'http_batch_ms':0, 'http_requests':0, 'asr_request_lock_ms':0}
        pcms = list(pcms)
        if any(len(pcm) % 2 for pcm in pcms):
            raise ValueError('PCM须为16位小端格式')
        output = [''] * len(pcms)
        nonempty = [(i, pcm) for i, pcm in enumerate(pcms) if pcm]
        if not nonempty:
            return output
        if not self.model:
            raise RuntimeError('Qwen服务尚未检查模型列表')
        if not self.batch_supported:
            raise RuntimeError('Qwen服务未提供audio_batch接口，请启动项目的Qwen批转写服务')
        # One batch request in flight, including when source separation submits audio.
        lock_started = monotonic()
        with self.request_lock:
            self.last_timing['asr_request_lock_ms'] = elapsed_ms(monotonic(), lock_started)
            for offset in range(0, len(nonempty), self.batch_size):
                group = nonempty[offset:offset+self.batch_size]
                items = []
                for n, (_, pcm) in enumerate(group, 1):
                    buffer = io.BytesIO()
                    with wave.open(buffer, 'wb') as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes(pcm)
                    url = 'data:audio/wav;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
                    items.append({'id': f's{n}', 'audio_url': url})
                request_started = monotonic()
                result = self._request('/asr/batch', {'model': self.model, 'items': items})
                self.last_timing['http_batch_ms'] = round(self.last_timing['http_batch_ms'] + elapsed_ms(monotonic(), request_started), 1)
                self.last_timing['http_requests'] += 1
                try:
                    rows = result['results']
                    if not isinstance(rows, list) or len(rows) != len(items):
                        raise ValueError('count mismatch')
                    texts = {}
                    for row in rows:
                        sid, text = row['id'], row['text']
                        if not isinstance(sid, str) or sid in texts or not isinstance(text, str):
                            raise ValueError('invalid batch item')
                        if row.get('finish_reason') == 'length':
                            raise RuntimeError('Qwen ASR输出被截断，请缩短音频或调整服务限制')
                        texts[sid] = clean_asr_text(text.split('<asr_text>', 1)[-1].strip())
                    if set(texts) != {item['id'] for item in items}:
                        raise ValueError('batch references mismatch')
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError('Qwen ASR批量返回格式异常或片段对应不完整') from exc
                for n, (index, _) in enumerate(group, 1):
                    output[index] = texts[f's{n}']
        return output

    def transcribe_pcm(self, pcm):
        return self.transcribe_pcm_many([pcm])[0]

    def transcribe_many(self, audios):
        import numpy as np
        pcms = [(np.clip(audio, -1, 32767 / 32768) * 32768).astype('<i2').tobytes() for audio in audios]
        return self.transcribe_pcm_many(pcms)

    async def transcribe_sources(self, pcms):
        texts = await asyncio.to_thread(self.transcribe_pcm_many, pcms)
        return [[Transcript(text, 'source', 0, len(pcm) // 32, True)] if text else []
                for pcm, text in zip(pcms, texts)]

    async def transcribe_source(self, pcm):
        return (await self.transcribe_sources([pcm]))[0]
