"""Independent frame producer; completed observations join the shared input entry."""
import asyncio
from pathlib import Path
from time import time


def install_vision(app, process, subject_id, deliver, *, voice_active=lambda: True):
    from aiohttp import web
    vision = process.vision
    queue = asyncio.Queue(maxsize=1)
    job = None
    last_sample = {}

    def same_origin(request):
        if request.headers.get('Origin') != f'http://{request.host}':
            raise web.HTTPForbidden(text='视觉操作需要同源请求')

    async def page(request):
        return web.FileResponse(Path(__file__).with_name('vision_ui.html'))

    async def state(request):
        environment = await asyncio.to_thread(vision.store.latest_sample, subject_id)
        errors = await asyncio.to_thread(vision.store.errors, subject_id)
        return web.json_response({'environment':environment,
            'sample_seconds':vision.config.sample_seconds, 'model':vision.config.model,
            'errors':errors, 'server_time':time(), 'voice_active':voice_active()}, headers={'Cache-Control':'no-store'})

    async def submit(request):
        same_origin(request)
        if not voice_active():
            raise web.HTTPConflict(text='视觉模式需要先开启语音，请在交谈页面选择视觉模式')
        source = request.headers.get('X-Visual-Source', 'browser')[:80]
        if not source:
            raise web.HTTPBadRequest(text='缺少画面来源')
        if time() - last_sample.get(source, 0) < vision.config.sample_seconds:
            raise web.HTTPTooManyRequests(text='采样过于频繁')
        try:
            from jshi.core.media import map_source_time
            source_time = float(request.headers.get('X-Captured-At', time()))
            offset = float(request.headers.get('X-Clock-Offset', '0'))
            uncertainty = float(request.headers.get('X-Clock-Uncertainty', '0'))
            captured = map_source_time(source_time, offset)
            import math
            if not math.isfinite(uncertainty) or uncertainty < 0 or not math.isfinite(captured) or captured <= 0 or captured > time()+60:
                raise ValueError('invalid capture time')
            data = await request.read()
            if len(data) > vision.config.max_image_bytes:
                raise ValueError('图片过大')
            await asyncio.to_thread(vision.submit, subject_id, data, captured, source,
                clock={'source_time':source_time,'clock_offset':offset,'clock_uncertainty':uncertainty})
            last_sample[source] = time()
        except (ValueError, RuntimeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({'accepted':True}, status=202)

    async def photo(request):
        try:
            data = await asyncio.to_thread(vision.store.image, request.match_info['frame'], subject_id)
        except KeyError:
            raise web.HTTPNotFound(text='照片不可用')
        return web.Response(body=data, content_type='image/jpeg', headers={'Cache-Control':'no-store'})

    async def memories(request):
        return web.json_response({'observations':await asyncio.to_thread(vision.store.memories, subject_id)}, headers={'Cache-Control':'no-store'})

    async def interpret(request):
        same_origin(request)
        try:
            payload = await request.json()
            frame = str(payload['frame'])
            question = str(payload.get('question', '重新描述照片里的环境'))[:1000]
            if not await asyncio.to_thread(vision.request_action, subject_id, f'回看照片 {frame}：{question}'):
                raise ValueError('照片不可用或观察任务正忙')
        except (ValueError, KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({'accepted':True}, status=202)

    async def watch():
        while True:
            environment = await queue.get()
            try:
                await deliver(environment, {})
            except Exception as exc:
                await asyncio.to_thread(vision.store.error, subject_id, exc)
            finally:
                queue.task_done()

    async def start(app):
        nonlocal job
        loop = asyncio.get_running_loop()
        def enqueue(s):
            if job is None or job.done():
                return
            if queue.full():
                queue.get_nowait()
                queue.task_done()
            queue.put_nowait(s)
        vision.on_update = lambda s: loop.call_soon_threadsafe(enqueue, s)
        job = asyncio.create_task(watch())

    async def close(app):
        vision.on_update = None
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await asyncio.to_thread(vision.close)

    app.on_startup.append(start)
    app.on_cleanup.append(close)
    app.router.add_get('/vision', page)
    app.router.add_get('/api/vision', state)
    app.router.add_post('/api/vision/frame', submit)
    app.router.add_get('/api/vision/photo/{frame}', photo)
    app.router.add_get('/api/vision/memories', memories)
    app.router.add_post('/api/vision/interpret', interpret)
