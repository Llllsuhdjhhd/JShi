"""Composition root: choose models and assemble the audio pipeline."""
from __future__ import annotations
import os
import hashlib
from pathlib import Path
from .pipeline import LocalASR
from .speakers import LocalSpeakers
from .model_assets import ASR_NAME, REFINER_NAME, SEGMENTATION_NAME, SPEAKER_MODELS

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
            num_threads=threads, provider="cpu", language="auto", use_itn=True,
        )
    print("本地识别：流式预览 + SenseVoice 整句复核" if refiner else "本地识别：小模型流式识别（可运行 voice-setup --refine 升级整句识别）")
    diarizer = None
    segment_model = root / SEGMENTATION_NAME / "model.onnx"
    if segment_model.is_file() and refiner is not None and speakers is not None:
        from .diarization import LocalDiarizer

        segmenter = build_segmenter_factory(root, threads)
        model = os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++')
        diarizer = LocalDiarizer(segmenter(model), speakers, refiner, build_engine=segmenter, model=model)
        print("说话人时间线：本地分段，最长 6 秒一个处理窗（不是音源分离）")
    result = LocalASR(recognizer, speakers, refiner, diarizer)
    result.backend = 'local'
    return result


def build_segmenter_factory(root, threads):
    import sherpa_onnx
    segment_model = root / SEGMENTATION_NAME / "model.onnx"
    def segmenter(model):
        speaker_model = root / SPEAKER_MODELS[model]
        if not speaker_model.is_file():
            raise ValueError(f"声纹模型 {model} 尚未准备")
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segment_model)),
                num_threads=threads, provider="cpu"),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model), num_threads=threads, provider="cpu"),
            clustering=sherpa_onnx.FastClusteringConfig(threshold=.5),
        )
        if not cfg.validate():
            raise ValueError("说话人分段模型配置无效")
        return sherpa_onnx.OfflineSpeakerDiarization(cfg)
    return segmenter


def build_qwen(data_dir, profiles, config):
    from .qwen import QwenRecognizer
    from .pipeline import SpeechPipeline
    from .analysis import SpeakerAnalyzer
    from .segmentation import SileroSegmenter, segment_settings
    from .turns import TurnCoordinator, build_turn_policy
    from .semantic_turn import build_semantic_policy
    from .model_assets import VAD_NAME
    import sherpa_onnx
    root = Path(os.getenv('JSHI_VOICE_LOCAL_DIR', str(data_dir / 'voice_models')))
    if not (root / VAD_NAME).is_file():
        raise ValueError('Silero VAD模型未准备，请运行 jshi voice-setup --vad --diarize')
    speakers = build_speakers(data_dir, profiles)
    if speakers is None or not (root / SEGMENTATION_NAME / 'model.onnx').is_file():
        raise ValueError('本地声纹或多人分段模型未准备，请运行 jshi voice-setup --vad --diarize')
    threads = max(1, min(4, int(os.getenv('JSHI_VOICE_LOCAL_THREADS', '2'))))
    factory = build_segmenter_factory(root, threads)
    model = os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++')
    analyzer = SpeakerAnalyzer(factory(model), speakers, build_engine=factory, model=model)
    vad = sherpa_onnx.VadModelConfig()
    vad.silero_vad.model = str(root / VAD_NAME)
    settings = segment_settings(config)
    vad.silero_vad.min_silence_duration = settings.silence_ms / 1000
    vad.silero_vad.max_speech_duration = settings.max_window_s
    vad.sample_rate = 16000
    vad.num_threads = threads
    detector = SileroSegmenter(sherpa_onnx.VoiceActivityDetector(vad, buffer_size_in_seconds=30),
                               max_window_s=settings.max_window_s)
    transcriber = QwenRecognizer(config)
    transcriber.check()
    coordinator = TurnCoordinator(build_turn_policy(config, root),
        semantic=build_semantic_policy(config,root),
        timeout_s=config.turn_timeout_s, max_hold_s=config.turn_max_hold_s,
        semantic_timeout_s=config.semantic_turn_timeout_s)
    return SpeechPipeline(detector, analyzer, transcriber, backend='qwen', turns=coordinator)

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
        enrollment_threshold=float(os.getenv('JSHI_VOICE_ENROLL_THRESHOLD', '.8')),
        anonymous_ttl_s=float(os.getenv('JSHI_VOICE_ANONYMOUS_DAYS', '7')) * 86400)


