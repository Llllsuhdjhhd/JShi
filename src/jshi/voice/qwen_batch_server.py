"""WSL service: one HTTP batch maps to one native Qwen transcribe(audio=[...]).

Launch via tools/start_qwen_batch_wsl.sh. No Windows, speaker or capture dependencies.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from threading import Lock
import wave


class BatchTranscription:
    def __init__(self, model, model_name, *, max_batch_size=8):
        if not 1 <= max_batch_size <= 32:
            raise ValueError('batch size must be 1–32')
        self.model, self.model_name = model, model_name
        self.max_batch_size = max_batch_size
        self.lock = Lock()

    def models(self):
        return {'object': 'list', 'data': [{'id': self.model_name, 'object': 'model',
            'capabilities': {'audio_batch': True, 'max_batch_size': self.max_batch_size}}]}

    def transcribe(self, payload):
        import numpy as np
        if payload.get('model') != self.model_name:
            raise ValueError('unknown model')
        items = payload.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= self.max_batch_size:
            raise ValueError(f'items must contain 1–{self.max_batch_size} audio segments')
        audios, ids = [], []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('invalid audio item')
            sid, url = item.get('id'), item.get('audio_url')
            if not isinstance(sid, str) or not 1 <= len(sid) <= 16 or sid in ids:
                raise ValueError('audio item IDs must be unique short strings')
            if not isinstance(url, str) or not url.startswith('data:audio/wav;base64,'):
                raise ValueError('audio must be a WAV data URL')
            try:
                raw = base64.b64decode(url.split(',', 1)[1], validate=True)
                with wave.open(io.BytesIO(raw), 'rb') as wav:
                    if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, 'NONE'):
                        raise ValueError('audio must be mono 16kHz 16-bit PCM WAV')
                    if not 1 <= wav.getnframes() <= 30 * 16000:
                        raise ValueError('each audio segment must be >0 and <=30 seconds')
                    pcm = wav.readframes(wav.getnframes())
                    if len(pcm) != wav.getnframes() * 2:
                        raise ValueError('truncated WAV')
            except (binascii.Error, wave.Error, EOFError) as exc:
                raise ValueError('invalid WAV data') from exc
            ids.append(sid)
            audios.append((np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768, 16000))
        with self.lock:
            # All independent audio inputs enter one native batch inference call.
            results = self.model.transcribe(audio=audios, language=None, return_time_stamps=False)
        if len(results) != len(items):
            raise RuntimeError('model returned an incomplete batch')
        rows = []
        for sid, result in zip(ids, results):
            if not isinstance(result.text, str):
                raise RuntimeError('model returned a non-text result')
            rows.append({'id': sid, 'text': result.text, 'language': result.language})
        return {'model': self.model_name, 'results': rows}


def make_server(address, service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == '/v1/models':
                self.reply(200, service.models())
            elif self.path == '/health':
                self.reply(200, {'status': 'ready'})
            else:
                self.reply(404, {'error': 'unknown endpoint'})

        def do_POST(self):
            if self.path != '/v1/asr/batch':
                self.reply(404, {'error': 'use POST /v1/asr/batch'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16 * 1024 * 1024:
                    raise ValueError('request size must be 1–16MiB')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('request must be a JSON object')
                self.reply(200, service.transcribe(payload))
            except (ValueError, KeyError) as exc:
                self.reply(400, {'error': str(exc)})
            except Exception:
                # Do not return input audio or internal model traces to clients.
                import logging
                logging.exception('Qwen batch inference failed')
                self.reply(500, {'error': 'batch inference failed'})
    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--batch-size', type=int, default=8, choices=range(1, 33))
    parser.add_argument('--gpu-memory-utilization', type=float, default=.85)
    args = parser.parse_args()
    from qwen_asr import Qwen3ASRModel
    model = Qwen3ASRModel.LLM(model=args.model, gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=8192, max_inference_batch_size=args.batch_size,
        max_new_tokens=512, max_num_seqs=args.batch_size,
        max_num_batched_tokens=2048, limit_mm_per_prompt={'audio': 1})
    service = BatchTranscription(model, args.model, max_batch_size=args.batch_size)
    server = make_server((args.host, args.port), service)
    print(f'Qwen batch service ready on {args.host}:{args.port}, batch_size={args.batch_size}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
