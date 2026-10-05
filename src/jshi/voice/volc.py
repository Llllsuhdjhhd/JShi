"""Small adapters for documented Volcengine ASR / TTS / voice registration.

Network dependencies are optional and loaded only by the voice application.
"""
from __future__ import annotations

import base64
import gzip
import json
import struct
from dataclasses import dataclass, replace
from uuid import uuid4

from .config import VoiceConfig


def pack_asr(payload: bytes, *, audio: bool = False, final: bool = False) -> bytes:
    compressed = gzip.compress(payload)
    flags = 2 if final else 0
    header = bytes((0x11, ((2 if audio else 1) << 4) | flags,
                    0x01 if audio else 0x11, 0))
    return header + struct.pack(">I", len(compressed)) + compressed


def unpack_asr(frame: bytes) -> tuple[dict, bool]:
    if len(frame) < 8 or frame[0] >> 4 != 1:
        raise ValueError("invalid ASR frame")
    offset = (frame[0] & 15) * 4
    message_type, flags = frame[1] >> 4, frame[1] & 15
    code = 0
    if message_type == 15:
        code = struct.unpack_from(">I", frame, offset)[0]
        offset += 4
    elif message_type == 9:
        if flags & 1:
            offset += 4  # response sequence
    else:
        raise ValueError("unsupported ASR response type")
    if len(frame) < offset + 4:
        raise ValueError("truncated ASR frame")
    size = struct.unpack_from(">I", frame, offset)[0]
    payload = frame[offset + 4:]
    if len(payload) != size:
        raise ValueError("truncated ASR payload")
    if frame[2] & 15 == 1:
        payload = gzip.decompress(payload)
    data = json.loads(payload) if frame[2] >> 4 == 1 else {"message": payload.decode("utf-8", "replace")}
    if code:
        raise RuntimeError(f"ASR error {code}: {data.get('message', 'request failed')}")
    if data.get("code", 0) not in (0, 1000, 20000000):
        raise RuntimeError(f"ASR error {data.get('code')}")
    return data.get("payload_msg", data), bool(flags & 2 or data.get("is_last_package"))


@dataclass(frozen=True)
class Transcript:
    text: str
    track_id: str
    start_ms: int
    end_ms: int
    final: bool
    overlap: bool = False
    voiceprint_id: str = ""
    confidence: float | None = None
    input_id: str = ""
    speaker_cluster_id: str = ""  # local anonymous cluster, not a named identity
    identity_note: str = ""
    identity_uncertain: bool = False
    identity_tentative: bool = False  # session continuity, not a fresh named match


class TranscriptAssembler:
    def __init__(self, *, identity_mode: str = "diarization") -> None:
        self.identity_mode = identity_mode
        self.seen: set[tuple] = set()
        self.ranges: list[tuple[int, int, str]] = []
        self.expired_before_ms = 0

    @staticmethod
    def covered(start: int, end: int, ranges) -> bool:
        if end <= start:
            return False
        # A newly arriving speaker can speak wholly inside another person's
        # interval. Only shared committed boundaries justify treating a range
        # as a revision/merge of previously delivered audio.
        if not any(a == start for a, _, _ in ranges) or not any(b == end for _, b, _ in ranges):
            return False
        cursor = start
        gaps = 0
        for a, b, _ in sorted(ranges):
            if b <= cursor:
                continue
            if a > cursor:
                gaps += a - cursor
                # Re-segmentation can absorb short pauses between old words.
                if a - cursor > 1200 or gaps > (end - start) * .2:
                    return False
            cursor = max(cursor, b)
            if cursor >= end:
                return True
        return False

    def accept(self, payload: dict) -> tuple[Transcript, ...]:
        result = payload.get("result", {})
        results = result if isinstance(result, list) else [result]
        outputs = []
        # Compare against earlier packets only: simultaneous new speakers in
        # this packet must both reach the scene, including overlapping speech.
        committed = tuple(self.ranges)
        for r in results:
            utterances = r.get("utterances", [])
            for u in utterances:
                text = str(u.get("text", "")).strip()
                if not text:
                    continue
                start, end = int(u.get("start_time", 0)), int(u.get("end_time", 0))
                additions = u.get("additions") or {}
                sid = str(additions.get("speaker_id", u.get("speaker_id", "")))
                # Unknown or absent speaker id must not bind successive people.
                track = sid if sid and sid not in {"-1", "unknown"} else f"unidentified-{start}"
                final = u.get("definite") is True
                if end > start and (end <= self.expired_before_ms or self.covered(start, end, committed)):
                    continue
                # Final text is immutable. A later speaker-label revision must
                # not deliver the same timed words as another conversation turn.
                key = (start, end, text)
                if final and key in self.seen:
                    continue
                overlap = bool(u.get("overlap", additions.get("overlap", False))) or any(
                    a < end and start < b and other != track for a, b, other in self.ranges)
                # Only registered ids supplied by voiceprint mode can be used as
                # permanent identity evidence. Diarization numbers are never ids.
                vpid = sid if self.identity_mode == "voiceprint" and sid and sid not in {"-1", "unknown"} else ""
                outputs.append(Transcript(text, track, start, end, final, overlap, vpid))
                if final:
                    self.seen.add(key)
                    self.ranges.append((start, end, track))
        # Session ASR frames repeat the history; bound bookkeeping by time.
        if self.seen:
            cutoff = max(self.expired_before_ms, max(k[1] for k in self.seen) - 60000)
            self.expired_before_ms = cutoff
            self.seen = {k for k in self.seen if k[1] >= cutoff}
            self.ranges = [r for r in self.ranges if r[1] >= cutoff]
        return tuple(replace(t, overlap=True) if any(
            other.track_id != t.track_id and other.start_ms < t.end_ms and t.start_ms < other.end_ms
            for other in outputs) else t for t in outputs)


class VolcVoice:
    def __init__(self, config: VoiceConfig, http) -> None:
        self.config, self.http = config, http
        self._tts = None

    def headers(self, resource: str = "") -> dict:
        headers = {"X-Api-Key": self.config.api_key, "X-Api-Request-Id": str(uuid4())}
        if resource:
            headers["X-Api-Resource-Id"] = resource
        return headers

    def asr_request(self) -> dict:
        request = {"model_name": "bigmodel", "enable_nonstream": True,
                   "show_utterances": True, "enable_speaker_info": True,
                   "enable_ddc": False, "end_window_size": self.config.end_window_ms}
        if self.config.identity_mode == "voiceprint":
            request.update(ssd_mode=2, voiceprints=[{"id": "", "group_id": self.config.voiceprint_group}])
        else:
            request["ssd_version"] = "200"
        return {"user": {"uid": "jshi-voice"},
                "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
                "request": request}

    async def open_asr(self):
        headers = self.headers(self.config.asr_resource)
        headers["X-Api-Connect-Id"] = str(uuid4())
        ws = await self.http.ws_connect(
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
            headers=headers, heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.send_bytes(pack_asr(json.dumps(self.asr_request()).encode()))
        return ws

    def tts_request(self, text: str) -> dict:
        t = self.config.template
        return {"user": {"uid": "jshi-voice"}, "req_params": {
            "text": text, "speaker": t.speaker,
            "audio_params": {"format": "pcm", "sample_rate": t.sample_rate, "speech_rate": t.speech_rate},
            "additions": json.dumps({"context_texts": [t.direction], "disable_markdown_filter": False}, ensure_ascii=False)}}

    async def synthesize(self, text: str):
        if self.config.tts_transport == 'websocket':
            async def texts():
                yield text
            stream = self.synthesize_stream(texts())
            try:
                async for pcm in stream:
                    yield pcm
            finally:
                await stream.aclose()
            return
        async for pcm in self.synthesize_sse(text):
            yield pcm

    async def synthesize_stream(self, texts):
        from .tts_ws import BidirectionalTTS
        if self._tts is None:
            self._tts = BidirectionalTTS(self.http, lambda: self.headers(self.config.tts_resource), self.tts_request)
        stream = self._tts.stream(texts)
        try:
            async for pcm in stream:
                yield pcm
        finally:
            await stream.aclose()

    async def close(self):
        if self._tts is not None:
            await self._tts.close()

    async def synthesize_sse(self, text: str):
        import aiohttp
        got_audio = False
        async with self.http.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
            headers=self.headers(self.config.tts_resource), json=self.tts_request(text),
            timeout=aiohttp.ClientTimeout(total=60)) as response:
            response.raise_for_status()
            async for line in response.content:
                if not line.startswith(b"data:"):
                    continue
                data = json.loads(line[5:].strip())
                code = data.get("code", 0)
                if code not in {0, 20000000}:
                    raise RuntimeError(f"TTS error {code}: {data.get('message', '')}")
                if data.get("data"):
                    got_audio = True
                    yield base64.b64decode(data["data"], validate=True)
                if code == 20000000:
                    break
        if not got_audio:
            raise RuntimeError("TTS returned no audio")

    async def register(self, name: str, audio_url: str) -> str:
        if not audio_url.startswith("https://"):
            raise ValueError("声纹注册需要云端可下载的 HTTPS 音频地址")
        body = {"Action": 0, "AudioUrl": audio_url, "SpeakerName": name}
        if self.config.voiceprint_group:
            body["GroupId"] = self.config.voiceprint_group
        async with self.http.post("https://openspeech.bytedance.com/api/proxy/invoke?Action=UpdateVoiceprint",
            headers=self.headers(), json=body) as response:
            response.raise_for_status()
            data = await response.json()
        result = data.get("Result", {})
        if data.get("ResponseMetadata", {}).get("Error") or result.get("Code") != 1000 or not result.get("SpeakId"):
            raise RuntimeError("声纹注册失败：" + str(result.get("Code", "unknown")))
        return str(result["SpeakId"])
