"""Start the offline MLX server and prefill the default cognition system prefix."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from jshi.app.model_settings import MLX_MODEL
from jshi.core import SubjectState
from jshi.models.base import ModelRequest, EchoModel
from jshi.models.prompt import build_system
from jshi.models.routing import ProviderModel
from jshi.skill.cognition import CognitionSkill
from jshi.voice.jev_serial import split_instruction
from jshi.style.packs import SMITH, reply_instruction_for, reply_schema_for, zone_chars_for


def main():
    root = Path(os.environ.get('JSHI_LOCAL_AI_DIR', '~/local-ai')).expanduser()
    env = dict(os.environ, HF_HOME=str(root/'hf'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               APC_ENABLED='1', APC_DISK_ENABLED='0', APC_CHECKPOINT_INTERVAL_TOKENS='512',
               APC_CHECKPOINT_ENTRIES='32', APC_MEMORY_MAX_GB='3')
    started = monotonic()
    child = subprocess.Popen([root/'venv/bin/mlx_vlm.server', '--host', '127.0.0.1',
                              '--port', '18765', '--model', MLX_MODEL], env=env)
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    try:
        http = build_opener(ProxyHandler({}))
        while child.poll() is None:
            if monotonic()-started > 120:
                raise TimeoutError('MLX 启动超时')
            try:
                with http.open('http://127.0.0.1:18765/health', timeout=2) as response:
                    health = json.load(response)
            except (OSError, URLError):
                sleep(.25)
                continue
            if health.get('preload_failures'):
                raise RuntimeError('MLX 模型加载失败')
            if health.get('model_ready') and health.get('loaded_model') == MLX_MODEL:
                break
            sleep(.25)
        if child.poll() is not None:
            return child.returncode
        ready = monotonic()
        req = ModelRequest('subject_activity', '', SubjectState('', '', ''))
        skill = CognitionSkill(EchoModel())
        short = replace(req, persona_instruction=reply_instruction_for(
            SMITH, prompt_level=3, zone_chars=zone_chars_for(SMITH)),
            persona_schema=reply_schema_for(SMITH, prompt_level=3))
        def cognition_prefix(request):
            return build_system(replace(request, system_extra=skill.system_extra(request)))
        prefixes = [('木头主认知', cognition_prefix(req)), ('斯密斯短对话', cognition_prefix(short)),
                    ('现场主动主认知', cognition_prefix(replace(req, cognition_mode='initiative'))),
                    ('分步接话判断', split_instruction())]
        model = ProviderModel({'label':'本机 MLX', 'protocol':'mlx',
                               'endpoint':'http://127.0.0.1:18765/v1'}, MLX_MODEL, 'off', timeout=60)
        # Qwen's chat template requires a user turn. Discard the warmup token.
        print(f'MLX 服务就绪 {ready-started:.2f}s', flush=True)
        for label, prefix in prefixes:
            began = monotonic()
            request = model._payload([{'role':'system','content':prefix},
                                      {'role':'user','content':'。'}], max_tokens=1)
            with http.open(request, timeout=60) as response:
                usage = json.load(response)['usage']
            print(f'{label}预加载 {monotonic()-began:.2f}s，输入 {usage["prompt_tokens"]} tokens', flush=True)
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
