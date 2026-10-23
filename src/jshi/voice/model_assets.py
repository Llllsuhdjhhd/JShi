"""Model names and explicit model downloads; no audio processing."""
from pathlib import Path
import os

ASR_NAME = "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
SPEAKER_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
SPEAKER_MODELS = {'cam++': SPEAKER_NAME, 'eres2netv2': '3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx'}
REFINER_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0"

VAD_NAME = "silero_vad.onnx"

TTS_NAME = "vits-melo-tts-zh_en"

def setup_models(data_dir: Path, *, tts: bool = False, refine: bool = False, diarize: bool = False, vad: bool = False, turn: bool = False, semantic_turn: bool = False, speaker_model: str = 'cam++') -> None:
    """Fetch explicit, small official CPU models, with safe archive extraction."""
    from urllib.request import urlopen
    import tarfile
    import shutil
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    root.mkdir(parents=True, exist_ok=True)
    if semantic_turn:
        from huggingface_hub import snapshot_download
        from .semantic_turn import NAMO_REPO, NAMO_REVISION, NAMO_FILES, NamoSemanticPolicy
        snapshot_download(NAMO_REPO, revision=NAMO_REVISION,local_dir=root/'namo-turn',
                          allow_patterns=list(NAMO_FILES))
        NamoSemanticPolicy(root/'namo-turn')  # Validate model, tokenizer and warmup.
    if turn:
        from huggingface_hub import hf_hub_download
        import onnxruntime as ort
        name = 'smart-turn-v3.2-cpu.onnx'
        destination = root/name
        if not destination.is_file():
            downloaded = hf_hub_download('pipecat-ai/smart-turn-v3', name,
                revision='f766f81', local_dir=root/'smart-turn-download')
            ort.InferenceSession(downloaded, providers=['CPUExecutionProvider'])
            shutil.copyfile(downloaded, destination)
    if speaker_model not in SPEAKER_MODELS:
        raise ValueError('unknown speaker model')
    speaker_name = SPEAKER_MODELS[speaker_model]
    assets = [(ASR_NAME + ".tar.bz2", "asr-models"), (speaker_name, "speaker-recongition-models")]
    if vad and not (root / VAD_NAME).is_file():
        print(f"下载 {VAD_NAME} …", flush=True)
        tmp = root / (VAD_NAME + '.download')
        with urlopen('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx', timeout=60) as response, tmp.open('wb') as output:
            shutil.copyfileobj(response, output)
        tmp.replace(root / VAD_NAME)
    if tts:
        assets.append((TTS_NAME + ".tar.bz2", "tts-models"))
    if refine or diarize:
        assets.append((REFINER_NAME + ".tar.bz2", "asr-models"))
    if diarize:
        assets.append((SEGMENTATION_NAME + ".tar.bz2", "speaker-segmentation-models"))
    for name, tag in assets:
        destination = root / name
        if name == SEGMENTATION_NAME + '.tar.bz2' and (root / SEGMENTATION_NAME / 'model.onnx').is_file():
            continue
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
