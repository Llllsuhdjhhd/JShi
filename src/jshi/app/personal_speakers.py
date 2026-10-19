"""Web management for the auxiliary classifiers, independent of voiceprint registration."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from jshi.voice.personal_store import OTHER_VOICE


def install_personal_routes(app, service, profiles, speakers_for):
    from aiohttp import web

    def require_service():
        if service is None:
            raise ValueError('个人声音学习需要安装 voice-personal 可选依赖')

    async def page(request):
        return web.FileResponse(Path(__file__).with_name('personal_speakers.html'))

    async def status(request):
        if service is None:
            return web.json_response({'available': False, 'detail': '请安装 voice-personal 可选依赖'})
        value = await asyncio.to_thread(service.status)
        value['available'] = True
        value['people'] = [{'object_id': p.object_id, 'label': p.label} for p in profiles.list()
                           if p.status != 'rejected' and p.source not in {'voice_anonymous', 'voice_scene'}]
        value['samples'] = await asyncio.to_thread(service.store.samples, limit=100)
        return web.json_response(value, headers={'Cache-Control': 'no-store'})

    async def change(request):
        if request.headers.get('Origin', '') != f'http://{request.host}':
            raise web.HTTPForbidden(text='个人声音设置需要同源请求')
        try:
            require_service()
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError('请求须为对象')
            action = payload.get('action')
            oid = payload.get('object_id', '')
            if action == 'settings':
                await asyncio.to_thread(service.store.configure, payload.get('settings'))
            elif action == 'target':
                await asyncio.to_thread(service.set_target, oid, selected=payload.get('selected', True), pinned=payload.get('pinned', True))
            elif action == 'select':
                await asyncio.to_thread(service.select_targets, payload.get('object_ids'))
            elif action == 'train':
                service._person(oid)
                select_target = payload.get('select_target', False)
                if type(select_target) is not bool:
                    raise ValueError('选择训练人物的参数须为布尔值')
                if not service.store.settings()['enabled']:
                    return web.json_response({'queued': False, 'running': False, 'detail': '个人声音学习已暂停'})
                speakers = await speakers_for(payload.get('model', ''))
                if select_target and not any(t['object_id'] == oid for t in service.store.targets()):
                    await asyncio.to_thread(service.set_target, oid, selected=True, pinned=True)
                queued = service.request_training(oid, speakers, force=True)
                return web.json_response({'queued': queued, 'running': oid in service.training,
                                          'detail': '已提交后台训练' if queued else '该人物已有训练任务'})
            elif action == 'rollback':
                await asyncio.to_thread(service.rollback, oid)
            elif action == 'label':
                if oid != OTHER_VOICE:
                    service._person(oid)
                await asyncio.to_thread(service.store.relabel, str(payload.get('sample_id', '')), oid)
            elif action == 'delete':
                await asyncio.to_thread(service.store.delete, str(payload.get('sample_id', '')))
            elif action == 'sample':
                if oid != OTHER_VOICE:
                    service._person(oid)
                pcm = base64.b64decode(payload.get('pcm', ''), validate=True)
                speakers = await speakers_for(payload.get('model', ''))
                future = service.submit_capture(pcm, oid, str(payload.get('session_id', '')),
                    str(payload.get('input_id', '')), speakers, source='manual')
                if future is None:
                    raise ValueError('采集队列已满，请稍后再试')
                sid = await asyncio.wrap_future(future)
                return web.json_response({'sample_id': sid})
            else:
                raise ValueError('未知个人声音操作')
            return await status(request)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    app.router.add_get('/personal-speakers', page)
    app.router.add_get('/api/personal-speakers', status)
    app.router.add_post('/api/personal-speakers', change)
