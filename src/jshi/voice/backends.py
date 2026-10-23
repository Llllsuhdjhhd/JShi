"""One catalog shared by configuration, command line and application status."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ASRBackend:
    label: str
    transport: str
    analysis: str


ASR_BACKENDS = {
    'online': ASRBackend('火山流式 + 整句复核 + 多人时间线', 'cloud_stream', 'provider'),
    'local': ASRBackend('本地 CPU 识别', 'local', 'windows'),
    'deepgram': ASRBackend('Deepgram中文流式', 'cloud_stream', 'provider'),
    'qwen': ASRBackend('WSL Qwen3-ASR批转写 + Windows多人分段', 'batch', 'windows'),
}


def describe_asr(backend, config, pipeline=None):
    if backend == 'local':
        if getattr(pipeline, 'diarizer', None):
            return '本地说话人分段 + 整句识别'
        if getattr(pipeline, 'refiner', None):
            return '本地流式 + 整句复核'
        return '本地小模型流式'
    if backend == 'deepgram' and config.deepgram_diarize:
        return 'Deepgram中文流式 + 多人时间线'
    return ASR_BACKENDS[backend].label
