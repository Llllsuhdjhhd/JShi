"""CPU streaming recognition and speaker embeddings; no cloud audio upload."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from uuid import uuid4
from threading import RLock

from .volc import Transcript


ASR_NAME = "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
SPEAKER_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
SPEAKER_MODELS = {'cam++': SPEAKER_NAME, 'eres2netv2': '3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx'}
REFINER_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0"


def cosine(a, b) -> float:
    if len(a) != len(b) or len(a) == 0:
        return -1.0
    denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denom if denom else -1.0


class EnrollmentError(ValueError):
    def __init__(self, message, sample_indices=()):
        super().__init__(message)
        self.sample_indices = tuple(sample_indices)


class LocalSpeakers:
    def __init__(self, extractor, model_id: str, path: Path, profiles,
                 *, threshold: float = 0.65, margin: float = 0.08, enrollment_threshold: float = 0.8) -> None:
        self.extractor, self.model_id, self.path, self.profiles = extractor, model_id, path, profiles
        self.threshold, self.margin = threshold, margin
        self.enrollment_threshold = enrollment_threshold
        if not -1 <= threshold <= 1 or margin < 0:
            raise ValueError("invalid voice similarity thresholds")
        if not -1 <= enrollment_threshold <= 1:
            raise ValueError('invalid enrollment similarity threshold')
        self.lock = RLock()
        self.known: dict[str, dict] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("model_id") == model_id:
                self.known = data.get("entries", {})
        self.tracks: dict[str, list[float]] = {}
        self.track_samples: dict[str, list[list[float]]] = {}
        self.pending_tracks: list[list[float]] = []
        self.last_embedding: dict[str, list[float]] = {}
        self.binding: dict[str, str] = {}
        self.last_match = {}
        settings = self.path.with_suffix('.settings.json')
        if settings.is_file():
            saved = json.loads(settings.read_text(encoding='utf-8'))
            if saved.get('model_id') == model_id:
                value = saved.get('match_threshold')
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .3 <= value <= .95:
                    raise ValueError('声纹匹配门槛设置无效')
                self.threshold = float(value)

    def set_threshold(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .3 <= value <= .95:
            raise ValueError('声纹匹配门槛须在 0.30–0.95 之间')
        with self.lock:
            target = self.path.with_suffix('.settings.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps({'model_id': self.model_id, 'match_threshold': value}), encoding='utf-8')
            temp.replace(target)
            self.threshold = float(value)
            return self.threshold

    def embedding(self, samples):
        if self.extractor is None or len(samples) < 16000 * 1.5:
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        values = list(map(float, self.extractor.compute(stream)))
        if not values or not all(math.isfinite(x) for x in values):
            return None
        return values

    def identify(self, samples) -> tuple[str, str, float | None]:
        with self.lock:
            return self._identify(samples)

    def _identify(self, samples) -> tuple[str, str, float | None]:
        vector = self.embedding(samples)
        if vector is None:
            self.last_match = {'reason': '样本不足或无法提取声纹', 'seconds': round(len(samples)/16000, 2)}
            return f"unidentified-{uuid4().hex}", "", None
        by_person = {}
        for key, item in self.known.items():
            profile = self.profiles.get(item["object_id"])
            if profile is None or profile.status == "rejected":
                continue
            from .identity import canonical_voice_profile
            profile = canonical_voice_profile(self.profiles, profile)
            hit = (cosine(vector, item["embedding"]), key)
            if hit > by_person.get(profile.object_id, (-2, "")):
                by_person[profile.object_id] = hit
        known = sorted(by_person.values(), reverse=True)
        self.last_match = {'reason': '没有可用登记' if not known else '未达到匹配阈值' if known[0][0] < self.threshold else '与另一对象过于接近',
            'best_score': round(known[0][0], 3) if known else None,
            'runner_up': round(known[1][0], 3) if len(known)>1 else None,
            'threshold': self.threshold, 'margin': self.margin}
        if known and known[0][0] >= self.threshold and (len(known) < 2 or known[0][0]-known[1][0] >= self.margin):
            self.last_match['reason'] = '已匹配登记声纹'
            score, key = known[0]
            track = "known-" + key
            self.last_embedding[track] = vector
            self.binding[track] = self.known[key]["object_id"]
            return track, f"local:{self.model_id}:{key}", score
        candidates = sorted(((max(cosine(vector, sample) for sample in self.track_samples.get(track, [item])), track)
                             for track, item in self.tracks.items()), reverse=True)
        # Naming can be strict without fragmenting anonymous session objects.
        anonymous_threshold = min(.60, self.threshold)
        self.last_match.update(anonymous_threshold=anonymous_threshold,
            anonymous_best_score=round(candidates[0][0], 3) if candidates else None,
            anonymous_runner_up=round(candidates[1][0], 3) if len(candidates)>1 else None)
        if candidates and candidates[0][0] >= anonymous_threshold and (len(candidates)<2 or candidates[0][0]-candidates[1][0] >= self.margin):
            score, track = candidates[0]
            if len(samples) >= 48000 and score >= anonymous_threshold+self.margin:
                examples = self.track_samples.setdefault(track, [self.tracks[track]])
                examples.append(vector)
                # Keep the original anchor and a few recent clear matches.
                self.track_samples[track] = [examples[0], *examples[1:][-4:]]
            self.last_match['anonymous_reason'] = '沿用已有匿名声纹'
        else:
            # A failed identity match is not evidence of a new person. Leave
            # short/ambiguous/borderline observations unassigned.
            if len(samples) < 48000 or (candidates and candidates[0][0] >= max(.3, anonymous_threshold-.1)):
                self.last_match['anonymous_reason'] = '短句或匿名候选不明确，暂不新增访客'
                return f'unidentified-{uuid4().hex}', '', None
            if candidates:
                pending = next((i for i, item in enumerate(self.pending_tracks)
                                if cosine(vector, item) >= anonymous_threshold+self.margin), None)
                if pending is None:
                    self.pending_tracks = [*self.pending_tracks[-7:], vector]
                    self.last_match['anonymous_reason'] = '疑似不同声音，先占位；等待另一段较长发言支持，不新增访客'
                    return f'unidentified-{uuid4().hex}', '', None
                self.pending_tracks.pop(pending)
            score, track = None, "speaker-" + uuid4().hex[:10]
            self.tracks[track] = vector
            self.track_samples[track] = [vector]
            self.last_match['anonymous_reason'] = '足够长且未接近已有匿名声纹，建立临时访客'
        self.last_embedding[track] = vector
        # Session clustering alone does not claim an established identity.
        return track, "", score

    def bind(self, track: str, object_id: str) -> bool:
        with self.lock:
            return self._bind(track, object_id)

    def enroll_samples(self, clips, object_id: str) -> dict:
        """Extract one normalized centroid from manually selected clean clips."""
        import numpy as np
        vectors = []
        owners = []
        seconds = 0.0
        with self.lock:
            if self.profiles.get(object_id) is None:
                raise ValueError('对象不存在')
            durations = [len(pcm) / 32000 for pcm in clips]
            short = [i for i, d in enumerate(durations) if d < 1.5]
            if not clips or short:
                raise EnrollmentError('第 '+ '、'.join(str(i+1) for i in short)+' 段不足 1.5 秒；请取消这些短句，选择较长单人发言', short)
            if (len(clips) == 1 and durations[0] < 3) or (len(clips) > 1 and sum(durations) < 4):
                raise ValueError('单段至少 3 秒；多段合计至少 4 秒，请多选清晰发言')
            for index, pcm in enumerate(clips):
                values = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
                seconds += len(values) / 16000
                # Check inside long clips too, rather than hiding several
                # voices inside a single averaged embedding.
                for start in range(0, len(values), 48000):
                    window = values[start:start+48000]
                    if len(window) < 24000:
                        continue
                    vector = self.embedding(window)
                    if vector is not None:
                        vectors.append(vector)
                        owners.append((index, start / 16000))
                    else:
                        raise EnrollmentError(f'第 {index+1} 段 {start/16000:.1f} 秒处无法提取声纹；这不等于检测到了杂音，请换一段更完整的发言', [index])
            if not vectors:
                raise ValueError('有效声音不足，请再选择几段清晰发言（合计至少 1.5 秒）')
            normalized = []
            for v in vectors:
                a = np.asarray(v, dtype='float64')
                norm = np.linalg.norm(a)
                if norm <= 0 or not np.isfinite(norm):
                    raise ValueError('声纹特征无效，请换一组录音')
                normalized.append(a / norm)
            pairs = [(float(np.dot(a, b)), i, j) for i, a in enumerate(normalized) for j, b in enumerate(normalized) if j > i]
            weakest = min(pairs, default=None)
            minimum = weakest[0] if weakest else None
            if minimum is not None and minimum < self.enrollment_threshold:
                first, second = owners[weakest[1]], owners[weakest[2]]
                raise EnrollmentError(f'片段一致性不足：第 {first[0]+1} 段（{first[1]:.1f} 秒处）与第 {second[0]+1} 段（{second[1]:.1f} 秒处）相似度 {minimum:.3f}，登记要求 {self.enrollment_threshold:.3f}。短句、收声变化或混选人物都可能造成，不能据此判定有杂音；请检查这两段。', sorted({first[0], second[0]}))
            centroid = np.mean(normalized, axis=0)
            norm = np.linalg.norm(centroid)
            if norm <= 0:
                raise ValueError('无法合成声纹')
            track = 'manual-' + uuid4().hex
            self.last_embedding[track] = (centroid / norm).tolist()
            try:
                if not self._bind(track, object_id, basis='manual_selection'):
                    raise ValueError('声纹登记失败')
            finally:
                self.last_embedding.pop(track, None)
                self.binding.pop(track, None)
            return {'clips': len(clips), 'embeddings': len(vectors), 'seconds': round(seconds, 1),
                    'min_similarity': round(minimum, 3) if minimum is not None else None, 'enrollment_threshold': self.enrollment_threshold}

    def _bind(self, track: str, object_id: str, *, basis: str = 'local_self_introduction') -> bool:
        if self.binding.get(track) == object_id:
            return True
        vector = self.last_embedding.get(track)
        if vector is None or self.profiles.get(object_id) is None:
            return False
        key = uuid4().hex
        reference = f"local:{self.model_id}:{key}"
        from jshi.recognition import CarrierEntry
        self.profiles.add_carrier(object_id, CarrierEntry("voiceprint", reference, basis))
        entry = {"object_id": object_id, "embedding": vector}
        self.known[key] = entry
        self.binding[track] = object_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"model_id": self.model_id, "entries": self.known}), encoding="utf-8")
        temp.replace(self.path)
        return True


class LocalASR:
    def __init__(self, recognizer, speakers=None, refiner=None, diarizer=None) -> None:
        self.recognizer, self.speakers = recognizer, speakers
        self.refiner = refiner
        self.diarizer = diarizer
        self.early_speaker = None
        self.stream = recognizer.create_stream()
        self.offset_samples = 0
        self.segment_samples = 0
        self.samples = []
        self.last_text = ""

    def reset_session(self) -> None:
        self.stream = self.recognizer.create_stream()
        self.offset_samples = self.segment_samples = 0
        self.samples.clear()
        self.last_text = ""
        self.early_speaker = None
        if self.speakers:
            self.speakers.tracks.clear()
            self.speakers.track_samples.clear()
            self.speakers.pending_tracks.clear()
            self.speakers.last_embedding.clear()
            self.speakers.binding.clear()

    def feed(self, pcm: bytes, *, final: bool = False) -> tuple[Transcript, ...]:
        import numpy as np
        if len(pcm) % 2:
            raise ValueError("PCM must be signed 16-bit little endian")
        values = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
        if len(values):
            self.stream.accept_waveform(16000, values)
            self.samples.append(values)
            self.segment_samples += len(values)
        if final:
            self.stream.accept_waveform(16000, np.zeros(8000, dtype="float32"))
            self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        text = self.recognizer.get_result(self.stream).strip()
        endpoint = final or self.recognizer.is_endpoint(self.stream) or self.segment_samples >= 16000 * (6 if self.diarizer else 25)
        start, end = self.offset_samples // 16, (self.offset_samples+self.segment_samples) // 16
        output = []
        if endpoint and (text or (self.refiner is not None and self.samples)):
            audio = np.concatenate(self.samples) if self.samples else np.array([], dtype="float32")
            if self.diarizer is not None and len(audio):
                output.extend(self.diarizer.transcribe(audio, start))
                text = ""
            elif self.refiner is not None and len(audio):
                refined = self.refiner.create_stream()
                refined.accept_waveform(16000, audio)
                self.refiner.decode_stream(refined)
                text = refined.result.text.strip()
            text = re.sub(r"<\|.*?\|>|<unk>", "", text).strip()
            if text:
                track, vpid, confidence = self.speakers.identify(audio) if self.speakers else (f"unidentified-{start}", "", None)
                output.append(Transcript(text, track, start, end, True, voiceprint_id=vpid, confidence=confidence))
        elif text and text != self.last_text:
            if self.speakers and self.diarizer is None and self.segment_samples >= 24000 and self.early_speaker is None:
                self.early_speaker = self.speakers.identify(np.concatenate(self.samples))
            track, vpid, score = self.early_speaker or (f"pending-{start}", "", None)
            output.append(Transcript(text, track, start, end, False, voiceprint_id=vpid, confidence=score))
        self.last_text = text
        if endpoint and not final:
            self.recognizer.reset(self.stream)
            self.offset_samples += self.segment_samples
            self.segment_samples = 0
            self.samples.clear()
            self.last_text = ""
            self.early_speaker = None
        return tuple(output)


def build_speakers(data_dir: Path, profiles, *, model=None):
    model = model or os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++')
    if model not in SPEAKER_MODELS:
        raise ValueError('声纹模型须为 cam++ 或 eres2netv2')
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    speaker_model = root / SPEAKER_MODELS[model]
    if not speaker_model.is_file():
        return None
    try:
        import sherpa_onnx
    except ImportError:
        return None
    threads = max(1, min(4, int(os.getenv("JSHI_VOICE_LOCAL_THREADS", "2"))))
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model), num_threads=threads, provider="cpu")
    if not cfg.validate():
        raise ValueError("invalid local speaker model")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    model_id = hashlib.sha256(speaker_model.read_bytes()).hexdigest()[:16]
    bank = 'voiceprints.json' if model == 'cam++' else 'voiceprints-eres2netv2.json'
    return LocalSpeakers(extractor, model_id, data_dir / bank, profiles,
        threshold=float(os.getenv("JSHI_VOICE_MATCH_THRESHOLD", "0.65")),
        enrollment_threshold=float(os.getenv('JSHI_VOICE_ENROLL_THRESHOLD', '.8')))


def build_local(data_dir: Path, profiles) -> LocalASR:
    try:
        import sherpa_onnx
    except ImportError:
        raise ValueError('本地语音需要安装：python -m pip install -e ".[voice-local]"')
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    model_dir = root / ASR_NAME
    models = sorted(model_dir.glob("*.onnx"))
    tokens = model_dir / "tokens.txt"
    if len(models) != 1 or not tokens.is_file():
        raise ValueError("本地流式模型尚未准备。请先运行 jshi voice-setup，或设置 JSHI_VOICE_LOCAL_DIR。")
    threads = max(1, min(4, int(os.getenv("JSHI_VOICE_LOCAL_THREADS", "2"))))
    recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
        model=str(models[0]), tokens=str(tokens), num_threads=threads, provider="cpu",
        enable_endpoint_detection=True, rule1_min_trailing_silence=1.5,
        rule2_min_trailing_silence=1.2, rule3_min_utterance_length=25,
    )
    speaker_model = root / SPEAKER_NAME
    speakers = build_speakers(data_dir, profiles)
    refiner = None
    refine_mode = os.getenv("JSHI_VOICE_REFINE", "auto")
    if refine_mode not in {"auto", "on", "off"}:
        raise ValueError("JSHI_VOICE_REFINE 必须为 auto/on/off")
    refine_root = root / REFINER_NAME
    refine_models = sorted(refine_root.glob("*.onnx"))
    if refine_mode == "on" and not refine_models:
        raise ValueError("请先运行 jshi voice-setup --refine 下载整句识别模型")
    if refine_mode != "off" and refine_models:
        refiner = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(refine_models[0]), tokens=str(refine_root / "tokens.txt"),
            num_threads=threads, provider="cpu", language="zh", use_itn=True,
        )
    print("本地识别：流式预览 + SenseVoice 整句复核" if refiner else "本地识别：小模型流式识别（可运行 voice-setup --refine 升级整句识别）")
    diarizer = None
    segment_model = root / SEGMENTATION_NAME / "model.onnx"
    if segment_model.is_file() and refiner is not None and speakers is not None:
        from .diarization import LocalDiarizer
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segment_model)),
                num_threads=threads, provider="cpu"),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model), num_threads=threads, provider="cpu"),
            clustering=sherpa_onnx.FastClusteringConfig(threshold=.5),
        )
        if not cfg.validate():
            raise ValueError("说话人分段模型配置无效")
        diarizer = LocalDiarizer(sherpa_onnx.OfflineSpeakerDiarization(cfg), speakers, refiner)
        print("说话人时间线：本地分段，最长 6 秒一个处理窗（不是音源分离）")
    return LocalASR(recognizer, speakers, refiner, diarizer)


TTS_NAME = "vits-melo-tts-zh_en"


def setup_models(data_dir: Path, *, tts: bool = False, refine: bool = False, diarize: bool = False, speaker_model: str = 'cam++') -> None:
    """Fetch explicit, small official CPU models, with safe archive extraction."""
    from urllib.request import urlopen
    import tarfile
    import shutil
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    root.mkdir(parents=True, exist_ok=True)
    if speaker_model not in SPEAKER_MODELS:
        raise ValueError('unknown speaker model')
    speaker_name = SPEAKER_MODELS[speaker_model]
    assets = [(ASR_NAME + ".tar.bz2", "asr-models"), (speaker_name, "speaker-recongition-models")]
    if tts:
        assets.append((TTS_NAME + ".tar.bz2", "tts-models"))
    if refine or diarize:
        assets.append((REFINER_NAME + ".tar.bz2", "asr-models"))
    if diarize:
        assets.append((SEGMENTATION_NAME + ".tar.bz2", "speaker-segmentation-models"))
    for name, tag in assets:
        destination = root / name
        if (name == speaker_name and destination.is_file()) or (name.endswith(".tar.bz2") and (root / name.removesuffix(".tar.bz2") / "tokens.txt").is_file()):
            continue
        print(f"下载 {name} …", flush=True)
        tmp = root / (name + ".download")
        with urlopen(f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{tag}/{name}", timeout=60) as response, tmp.open("wb") as output:
            shutil.copyfileobj(response, output)
        if name.endswith(".tar.bz2"):
            with tarfile.open(tmp) as archive:
                members = archive.getmembers()
                for member in members:
                    resolved = (root / member.name).resolve()
                    if not resolved.is_relative_to(root.resolve()) or not (member.isfile() or member.isdir()):
                        raise ValueError("unsafe model archive")
                archive.extractall(root, members=members, filter="data")
            tmp.unlink()
        else:
            tmp.replace(destination)
    print(f"本地模型已准备：{root.resolve()}")
