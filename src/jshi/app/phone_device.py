"""HTTPS device listener. The desktop application owns recognition and playback state."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import ssl
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from weakref import WeakSet

from aiohttp import web

from .capture import CaptureSource


@dataclass
class PhoneListener:
    host: str
    port: int
    tls: ssl.SSLContext
    code: str

    @classmethod
    def from_args(cls, args):
        if not getattr(args, 'phone', None):
            return None
        if bool(args.phone_cert) != bool(args.phone_key):
            raise SystemExit('--phone-cert 与 --phone-key 需要同时指定。')
        if not 1 <= args.phone_port <= 65535 or args.phone_port == args.port:
            raise SystemExit('手机端口应在 1–65535 之间，并与桌面端口不同。')
        try:
            address = ipaddress.ip_address(args.phone)
            if address.is_unspecified:
                raise ValueError
        except ValueError:
            raise SystemExit('--phone 请填写本机局域网接口的具体 IP 地址。') from None
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        cert, key = args.phone_cert, args.phone_key
        if not cert:
            from .phone_tls import local_certificate
            try:
                cert, key, ca = local_certificate(args.data_dir/'phone-tls', str(address))
            except (OSError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc
            print(f'首次连接请将公开证书 {ca} 隔空投送到 iPhone，安装并开启完全信任。')
        tls.load_cert_chain(cert, key)
        return cls(str(address), args.phone_port, tls, f'{secrets.randbelow(10**8):08d}')

    @property
    def url(self):
        host = f'[{self.host}]' if ':' in self.host else self.host
        return f'https://{host}:{self.port}/'


# Only audio transport and playback control cross the device boundary.
PHONE_CONTROLS = {'speech_start', 'speech_end', 'played', 'stop', 'end', 'audio_clock'}
EVENT_FIELDS = {
    'ready': ('stream',), 'notice': ('text',), 'reply': ('reply_id', 'paused'),
    'audio': ('reply_id', 'segment', 'rate', 'pcm'), 'audio_end': ('reply_id',),
    'segment_end': ('reply_id', 'segment'),
    **{kind: ('reply_id', 'permit_seq', 'reason_id', 'cleared')
       for kind in ('pause', 'resume', 'stop', 'block_next', 'clear_block')},
}


def create_device_app(code, connection, submit_frame, vision_config, *, captures, voice_busy):
    cookie_name = 'jshi_phone'
    signing_key = secrets.token_bytes(32)
    attempts = deque()
    sockets = WeakSet()
    uploading = set()
    max_bytes = vision_config.max_image_bytes if vision_config else 1024
    app = web.Application(client_max_size=max(max_bytes + 1, 4096))

    def signature(value):
        return hmac.digest(signing_key, value.encode(), hashlib.sha256).hex()

    def paired(request):
        value, _, mac = request.cookies.get(cookie_name, '').partition('.')
        return len(value) == 32 and hmac.compare_digest(signature(value).encode(), mac.encode())

    def authorize(request):
        if request.headers.get('Origin') != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden(text='设备连接需要同源请求')
        if not paired(request):
            raise web.HTTPUnauthorized(text='请先输入主机上的配对码')

    async def pair(request):
        if request.headers.get('Origin') != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden()
        if request.content_length is not None and request.content_length != 8:
            raise web.HTTPBadRequest(text='配对码为八位数字')
        supplied = await request.read()
        now = monotonic()
        while attempts and now - attempts[0] >= 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise web.HTTPTooManyRequests(text='请在一分钟后重试')
        if not hmac.compare_digest(supplied, code.encode()):
            attempts.append(now)
            raise web.HTTPUnauthorized(text='配对码有误')
        value = secrets.token_hex(16)
        response = web.json_response({'paired': True})
        response.set_cookie(cookie_name, f'{value}.{signature(value)}', secure=True,
                            httponly=True, samesite='Strict', path='/')
        return response

    def source(request):
        owner = request.cookies[cookie_name]
        return CaptureSource('phone', hashlib.sha256(owner.encode()).hexdigest()[:16])

    async def info(request):
        is_paired = paired(request)
        state = 'available'
        if is_paired and voice_busy():
            state = 'this_device' if captures.active and captures.active.source == source(request) else 'in_use'
        return web.json_response({'paired': is_paired, 'server_time': time(), 'voice_state': state,
            'min_interval': vision_config.sample_seconds if vision_config else None},
            headers={'Cache-Control': 'no-store'})

    async def voice(request):
        authorize(request)
        if request.query:
            raise web.HTTPBadRequest(text='识别和播报使用主机设置')
        async def forward(ws, event):
            kind = event['type']
            if kind == 'notice' and not event.get('device_notice'):
                return
            if kind not in EVENT_FIELDS:
                return
            message = {k: event[k] for k in EVENT_FIELDS[kind] if k in event}
            await ws.send_json({'type': kind, **message})
        try:
            return await connection(request, source=source(request), device_send=forward,
                                    controls=PHONE_CONTROLS, sockets=sockets)
        except (web.HTTPBadRequest, web.HTTPConflict) as exc:
            # Browsers hide HTTP bodies on a failed WebSocket handshake.
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({'type': 'notice', 'text': exc.text})
            await ws.close()
            return ws

    async def frame(request):
        authorize(request)
        device = source(request)
        session = captures.resolve(device, request.headers.get('X-Device-Stream'))
        if session is None:
            raise web.HTTPConflict(text='本次设备连接已结束')
        if submit_frame is None:
            raise web.HTTPServiceUnavailable(text='主机没有开启视觉')
        lens = request.headers.get('X-Device-Lens')
        if lens not in ('user', 'environment') or request.content_type != 'image/jpeg':
            raise web.HTTPBadRequest(text='需要镜头来源与 JPEG 画面')
        if 'X-Captured-At' not in request.headers:
            raise web.HTTPBadRequest(text='缺少画面采集时间')
        if session in uploading:
            raise web.HTTPTooManyRequests(text='上一帧仍在接收')
        uploading.add(session)
        try:
            return await submit_frame(request, source=f'phone-{device.device_id}-{lens}',
                                      session=session, max_age=5)
        finally:
            uploading.discard(session)

    assets = {'/': 'phone.html', '/phone.js': 'phone.js', '/phone_camera.js': 'phone_camera.js',
              '/phone_playback.js': 'phone_playback.js', '/voice_capture.js': 'voice_capture.js'}
    async def asset(request):
        return web.FileResponse(Path(__file__).with_name(assets[request.path]),
            headers={'Cache-Control': 'no-cache', 'Referrer-Policy': 'no-referrer',
                     'X-Content-Type-Options': 'nosniff'})
    for path in assets:
        app.router.add_get(path, asset)
    app.router.add_post('/pair', pair)
    app.router.add_get('/api/device', info)
    app.router.add_get('/voice', voice)
    app.router.add_post('/api/device/frame', frame)

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets)))
    app.on_shutdown.append(shutdown)
    return app


def install_listener(desktop, config, connection, submit_frame, vision_config, *, captures, voice_busy):
    device = create_device_app(config.code, connection, submit_frame, vision_config, captures=captures, voice_busy=voice_busy)
    async def serve(app):
        runner = web.AppRunner(device, shutdown_timeout=5)
        await runner.setup()
        try:
            site = web.TCPSite(runner, config.host, config.port, ssl_context=config.tls)
            await site.start()
            print(f'手机设备入口 {config.url}   配对码 {config.code}', flush=True)
            yield
        finally:
            await runner.cleanup()
    desktop.cleanup_ctx.append(serve)
