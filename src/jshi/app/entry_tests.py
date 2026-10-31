"""Manual long-lived case library attached to the existing voice application."""
import asyncio
import io
from pathlib import Path
import wave


def register(app, root, recent_audio):
    from aiohttp import web
    from jshi.testing.entry_cases.store import CaseStore
    store = CaseStore(root)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, FileNotFoundError, wave.Error) as exc:
            if request.path.startswith('/api/entry-tests'):
                return web.json_response({'error': str(exc)}, status=400)
            raise
    app.middlewares.append(errors)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name('entry_tests.html'))

    async def cases(request):
        if request.method == 'GET':
            return web.json_response(store.list())
        return web.json_response(store.save(await request.json()))

    async def upload(request):
        ident = await asyncio.to_thread(store.import_wav, await request.read(), source={'kind': 'upload'})
        return web.json_response({'media': ident})

    async def retain(request):
        values = await request.json()
        start, end = int(values['start_ms']), int(values['end_ms'])
        if start < 0 or end <= start or end-start > 300000:
            raise ValueError('请选择不超过五分钟的有效连续区间')
        pcm = await asyncio.to_thread(recent_audio.segment, values['capture'], start, end)
        if pcm is None:
            raise ValueError('录音区间缺失或已清理')
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm)
        ident = await asyncio.to_thread(store.import_wav, output.getvalue(),
            source={'kind': 'recent_audio', 'capture': values['capture'], 'start_ms': start, 'end_ms': end})
        return web.json_response({'media': ident, 'timing_basis': 'reconstructed'})

    async def media(request):
        return web.FileResponse(store.path('media', request.match_info['ident'], 'original.wav'))

    app.router.add_get('/entry-tests', page)
    app.router.add_get('/api/entry-tests/cases', cases)
    app.router.add_post('/api/entry-tests/cases', cases)
    app.router.add_post('/api/entry-tests/media', upload)
    app.router.add_post('/api/entry-tests/retain', retain)
    app.router.add_get('/api/entry-tests/media/{ident}.wav', media)
