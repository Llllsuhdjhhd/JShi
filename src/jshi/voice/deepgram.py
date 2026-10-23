"""Deepgram Listen v1; speaker labels are session tracks, never identities."""
from __future__ import annotations

import re
from .transcripts import TranscriptAssembler


class DeepgramASR:
    def __init__(self, config, http):
        self.config, self.http = config, http

    async def open_asr(self):
        if not self.config.deepgram_api_key:
            raise ValueError('Deepgram识别缺少JSHI_DEEPGRAM_API_KEY')
        params = {'model':self.config.deepgram_model, 'language':self.config.deepgram_language,
            'encoding':'linear16', 'sample_rate':'16000', 'channels':'1',
            'interim_results':'true', 'punctuate':'true',
            'endpointing':str(self.config.end_window_ms)}
        if self.config.deepgram_diarize:
            params['diarize_model'] = 'v1'
        return await self.http.ws_connect('wss://api.deepgram.com/v1/listen',
            params=params, headers={'Authorization':'Token '+self.config.deepgram_api_key},
            heartbeat=20, max_msg_size=2*1024*1024)


def _words_text(words):
    text = ''
    for word in words:
        token = str(word.get('punctuated_word') or word.get('word') or '')
        if text and token and re.search(r'[A-Za-z0-9]$',text) and re.match(r'[A-Za-z0-9]',token):
            text += ' '
        text += token
    return text


class DeepgramAssembler:
    def __init__(self):
        self.assembler = TranscriptAssembler(identity_mode='diarization')

    def accept(self, data):
        kind = data.get('type')
        if kind == 'Error':
            raise RuntimeError('Deepgram识别错误：'+str(data.get('description') or data.get('variant') or 'unknown'))
        if kind != 'Results':
            return ()
        alternatives = data.get('channel',{}).get('alternatives',[])
        if not alternatives:
            return ()
        alternative = alternatives[0]
        text = str(alternative.get('transcript') or '').strip()
        if not text:
            return ()
        words = alternative.get('words') or []
        groups = []
        for word in words:
            speaker = word.get('speaker')
            if not groups or groups[-1][0] != speaker:
                groups.append((speaker,[]))
            groups[-1][1].append(word)
        utterances = []
        for speaker, group in groups:
            utterances.append({'text':text if len(groups)==1 else _words_text(group),
                'start_time':round(float(group[0]['start'])*1000),
                'end_time':round(float(group[-1]['end'])*1000),
                'speaker_id':f'deepgram-{speaker}' if isinstance(speaker,int) and speaker>=0 else '',
                'definite':data.get('is_final') is True})
        if not groups:
            start = round(float(data.get('start',0))*1000)
            utterances.append({'text':text,'start_time':start,
                'end_time':start+round(float(data.get('duration',0))*1000),
                'definite':data.get('is_final') is True})
        return self.assembler.accept({'result':{'utterances':utterances}})
