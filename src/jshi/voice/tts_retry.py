"""Retry a failed sentence only before any audio has left the server."""
import asyncio


def transient_error(exc):
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return isinstance(exc, RuntimeError) and 'connection closed' in str(exc).lower()


async def sentence_audio(cloud, text, *, is_current, on_retry):
    for attempt in range(2):
        sent = False
        stream = cloud.synthesize(text)
        try:
            async for pcm in stream:
                if not is_current():
                    return
                if pcm:
                    sent = True
                    yield pcm
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt or sent or not transient_error(exc) or not is_current():
                raise
            on_retry(type(exc).__name__)
        finally:
            await stream.aclose()
