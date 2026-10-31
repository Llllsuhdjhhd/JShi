"""Explicit live adapters; all writable state lives under the run directory."""
from pathlib import Path


def local_decoder(state):
    from jshi.recognition.profile import ObjectProfileRepository
    from jshi.voice.runtime import build_local
    state = Path(state); state.mkdir(parents=True, exist_ok=True)
    return build_local(state, ObjectProfileRepository(state/'profiles.sqlite3'))


def qwen_decoder(state):
    from jshi.recognition.profile import ObjectProfileRepository
    from jshi.voice.runtime import build_qwen
    from jshi.voice.config import VoiceConfig
    state = Path(state); state.mkdir(parents=True, exist_ok=True)
    return build_qwen(state, ObjectProfileRepository(state/'profiles.sqlite3'), VoiceConfig.from_env(asr='qwen'))


def live_gate(mode, state):
    from jshi.core.skillconfig import SkillConfigStore, build_model_port
    from jshi.voice.jev import VoiceJEV
    from jshi.decision import DecisionService
    from jshi.voice.jev_decision import entry_bridge
    state = Path(state); state.mkdir(parents=True, exist_ok=True)
    gate = VoiceJEV(build_model_port(SkillConfigStore().profile('voice_jev')), mode=mode)
    service = DecisionService(state)
    # The decision runtime remains off unless explicitly configured in this run.
    # Actual fallback path is retained in BatchDecision.stage_records.
    gate.replay_delivery = {'_decision_entry': entry_bridge(service)}
    gate.replay_close = service.close
    return gate
