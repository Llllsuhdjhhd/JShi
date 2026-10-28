"""Async native System One HTTP; one deadline, bounded body, no hidden retries."""
import asyncio
import json
from time import monotonic

from .base import DecisionResult, validate_answers, number


class SystemOnePort:
    def __init__(self, provider, model, endpoint, key, *, transport=None):
        self.provider, self.model, self.endpoint, self.key = provider, model, endpoint, key
        self.transport = transport
        self.session = None

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _post(self, payload):
        import aiohttp
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        async with self.session.post(self.endpoint, headers={'Authorization': 'Bearer ' + self.key},
                                     json=payload) as response:
            if response.status != 200:
                # Do not echo upstream errors, URLs, keys or private request state.
                return response.status, None
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise ValueError('response_size')
            return response.status, json.loads(body)

    async def decide(self, request, *, deadline):
        started = monotonic()
        def result(status, **kw):
            return DecisionResult(status, provider=self.provider, model=self.model,
                                  elapsed_ms=round((monotonic()-started)*1000, 2), **kw)
        if not self.key or not self.endpoint:
            return result('unavailable', reason='not_configured')
        if not 1 <= len(request.questions) <= 16:
            return result('invalid', reason='question_limit')
        payload = {'model': self.model, 'state': request.state, 'questions': request.questions}
        if len(json.dumps(payload, ensure_ascii=False)) > 32000:
            return result('invalid', reason='input_budget')
        try:
            async with asyncio.timeout(max(0, deadline-monotonic())):
                status, data = await (self.transport(payload) if self.transport else self._post(payload))
                if status != 200:
                    return result('unavailable', reason='http_' + str(status))
                answers = validate_answers(request.questions, data.get('answers'))
                server_ms = data.get('latency_ms')
                return DecisionResult('success', answers, self.provider, str(data.get('model', self.model)),
                    round((monotonic()-started)*1000, 2),
                    server_ms if number(server_ms, 0, 3600000) else None,
                    str(data.get('request_id', ''))[:160], data.get('usage', {}))
        except asyncio.TimeoutError:
            return result('timeout', reason='deadline')
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            return result('invalid', reason='response_contract')
        except Exception:
            return result('unavailable', reason='transport')
