"""Qwen Audio 3.1 ASR streaming over the documented DashScope duplex API."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .contracts import TimedWord
from uuid import uuid4
from .transcripts import TranscriptAssembler


class BailianAssembler:
    def __init__(self):
        self.assembler = TranscriptAssembler()

    def accept(self, data):
        header = data.get('header') or {}
        if header.get('event') == 'task-failed':
            raise RuntimeError('百炼语音识别失败：' + str(header.get('error_code', 'unknown')) + '：' + str(header.get('error_message', '')))
        if header.get('event') != 'result-generated':
            return ()
        sentence = (data.get('payload', {}).get('output') or {}).get('sentence') or {}
        if sentence.get('heartbeat') or not sentence.get('text'):
            return ()
        start, end = sentence.get('begin_time'), sentence.get('end_time')
        if type(start) is not int or start < 0:
            return ()
        final = sentence.get('sentence_end') is True
        if end is None and not final:
            words = sentence.get('words') or []
            end = max((w.get('end_time', start) for w in words if type(w.get('end_time')) is int), default=start)
        if type(end) is not int or end < start or final and end == start:
            return ()
        # sentence_id is a sentence identifier, NOT a speaker track.
        rows = self.assembler.accept({'result': {'utterances': [{
            'text': sentence['text'], 'start_time': start, 'end_time': end,
            'definite': final}]}})
        words = []
        previous = start
        for word in sentence.get('words') or ():
            a, b, text = word.get('begin_time'), word.get('end_time'), word.get('text')
            if type(a) is not int or type(b) is not int or not isinstance(text, str) or not start <= a < b <= end or a < previous:
                words = []
                break
            punctuation = word.get('punctuation') or ''
            if punctuation and not text.endswith(punctuation):
                text += punctuation
            words.append(TimedWord(text, a, b))
            previous = b
        return tuple(replace(row, words=tuple(words)) for row in rows)


class BailianProtocol:
    wait_for_final = True

    def __init__(self, task_id):
        self.task_id = task_id
        self.assembler = BailianAssembler()

    async def send(self, ws, pcm):
        await ws.send_bytes(pcm)

    async def finish(self, ws):
        await ws.send_json({'header': {'action': 'finish-task', 'task_id': self.task_id, 'streaming': 'duplex'},
                            'payload': {'input': {}}})

    def decode(self, message):
        from aiohttp import WSMsgType
        if message.type != WSMsgType.TEXT:
            return None
        data = message.json()
        if data.get('header', {}).get('task_id') != self.task_id:
            raise RuntimeError('百炼语音识别返回的任务标识不匹配')
        return data, data.get('header', {}).get('event') == 'task-finished'


async def open_bailian(config, http):
    from aiohttp import WSMsgType
    from .streaming_asr import StreamingASRSession
    if not config.bailian_asr_api_key:
        raise ValueError('百炼识别缺少JSHI_BAILIAN_ASR_API_KEY或DASHSCOPE_API_KEY')
    task_id = str(uuid4())
    ws = await http.ws_connect(config.bailian_asr_url,
        headers={'Authorization': 'Bearer ' + config.bailian_asr_api_key},
        heartbeat=20, max_msg_size=2 * 1024 * 1024)
    try:
        parameters = {'format': 'pcm', 'sample_rate': 16000, 'heartbeat': True,
                      'vad_model': config.bailian_asr_vad_model,
                      'max_sentence_silence': config.end_window_ms,
                      'multi_threshold_mode_enabled': True}
        if config.asr_hotwords:
            parameters['vocabulary'] = {word: 3 for word in config.asr_hotwords}
        await ws.send_json({'header': {'action': 'run-task', 'task_id': task_id, 'streaming': 'duplex'},
                           'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                                       'model': config.bailian_asr_model, 'parameters': parameters, 'input': {}}})
        # The server must acknowledge the task BEFORE the microphone sends PCM.
        async with asyncio.timeout(15):
            while True:
                message = await ws.receive()
                if message.type != WSMsgType.TEXT:
                    raise RuntimeError('百炼识别在任务启动前断开连接')
                data = message.json()
                if data.get('header', {}).get('task_id') != task_id:
                    raise RuntimeError('百炼语音识别任务标识不匹配')
                BailianAssembler().accept(data)  # surfaces task-failed
                if data.get('header', {}).get('event') == 'task-started':
                    break
        return StreamingASRSession(ws, BailianProtocol(task_id))
    except BaseException:
        await ws.close()
        raise
