from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jshi.app import cli
from jshi.memory import (
    MemoryBatch,
    MemoryExperience,
    Rems3MemoryBackend,
    RemsUnavailableError,
    UnknownMemoryBackendError,
    build_memory_backend,
    rems_data_dir,
)
from jshi.memory.rems3 import (
    apply_jshi_llm_settings,
    batch_payload,
    build_rems_pipeline,
    from_ingest_result,
    from_recalled_fragments,
    openai_compat_base_url,
    quiet_embedding_logs,
)
from jshi.subject import SubjectRepository


class FakePipeline:
    def __init__(self) -> None:
        self.ingested = []
        self.queries = []

    def ingest_batch(self, batch):
        self.ingested.append(batch)
        return SimpleNamespace(
            subject_id=batch.subject_id,
            stored_marks={"seg-1": ["evt-1", "evt-2"]},
            sealed_event_ids=["evt-1", "evt-2"],
            role_ids=["OBJ-A"],
            unclosed_count=1,
            errors=["warn"],
        )

    def recall(self, subject_id, query, **kwargs):
        self.queries.append((subject_id, query, kwargs))
        return (
            SimpleNamespace(
                event_id="evt-1",
                text="原文",
                content="摘要",
                kind=None,
                object_id="OBJ-A",
                source_ids=["s1"],
                score=0.5,
                summary_level="L1",
            ),
        )


def _sample_batch() -> MemoryBatch:
    return MemoryBatch(
        batch_id="batch-keep-on-jshi",
        subject_id="stone",
        experiences=(
            MemoryExperience(
                subject_id="stone",
                text="你好",
                objects={"甲": "OBJ-A"},
                source_ids=("src-1",),
                segment_id="seg-1",
                origin="external",
            ),
        ),
        from_sequence=3,
        to_sequence=4,
        source_ids=("ledger-src",),
    )


def test_batch_payload_drops_ledger_fields():
    payload = batch_payload(_sample_batch())

    assert set(payload) == {"subject_id", "experiences"}
    assert payload["subject_id"] == "stone"
    experience = payload["experiences"][0]
    assert experience["text"] == "你好"
    assert experience["objects"] == {"甲": "OBJ-A"}
    assert experience["segment_id"] == "seg-1"
    assert "sub_segments" not in experience
    assert "batch_id" not in payload
    assert "from_sequence" not in payload


def test_from_ingest_result_lists_become_tuples():
    raw = SimpleNamespace(
        subject_id="stone",
        stored_marks={"seg-1": ["evt-1"]},
        sealed_event_ids=["evt-1"],
        role_ids=["OBJ-A"],
        unclosed_count=2,
        errors=["e"],
    )

    result = from_ingest_result(raw)

    assert result.sealed_event_ids == ("evt-1",)
    assert result.role_ids == ("OBJ-A",)
    assert result.errors == ("e",)
    assert result.stored_marks["seg-1"] == ("evt-1",)


def test_from_recalled_fragments_defaults_event_type():
    raw = (
        SimpleNamespace(
            event_id="evt-1",
            text="原文",
            content="",
            kind=None,
            object_id=None,
            source_ids=["s1"],
            score=1,
            summary_level=None,
        ),
    )

    fragments = from_recalled_fragments(raw)

    assert len(fragments) == 1
    assert fragments[0].event_type == "memory"
    assert fragments[0].kind == "fact"
    assert fragments[0].source_ids == ("s1",)
    assert fragments[0].text == "原文"


def test_prefer_neighbor_rems_puts_jshi_memory_first():
    from jshi.memory.rems3 import neighbor_rems_src, prefer_neighbor_rems

    neighbor = neighbor_rems_src()
    if neighbor is None:
        pytest.skip("本机没有并列的 Jshi_memory/src")
    prefer_neighbor_rems()
    assert Path(sys.path[0]).resolve() == neighbor.resolve()


def test_embedding_weights_present_requires_large_weight_file(tmp_path):
    from jshi.memory.rems3 import embedding_weights_present

    folder = tmp_path / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "x"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}", encoding="utf-8")
    assert embedding_weights_present("BAAI/bge-small-zh-v1.5", cache_root=tmp_path) is False
    (folder / "model.safetensors").write_bytes(b"0" * 1_000_001)
    assert embedding_weights_present("BAAI/bge-small-zh-v1.5", cache_root=tmp_path) is True


def test_disable_local_embedding_skips_semantic_without_torch(monkeypatch):
    from jshi.memory.rems3 import disable_local_embedding_if_needed

    monkeypatch.setenv("JSHI_REMS_SKIP_EMBEDDING", "1")
    called = {"semantic": 0, "index": 0}

    class Recall:
        def _semantic_route(self, *args, **kwargs):
            called["semantic"] += 1
            return {"evt": 1}

        def index_event(self, event) -> None:
            called["index"] += 1

    pipeline = SimpleNamespace(recall_pipeline=Recall())
    assert disable_local_embedding_if_needed(pipeline) is True
    assert pipeline.recall_pipeline._semantic_route("stone", "你好") == {}
    pipeline.recall_pipeline.index_event(object())
    assert called == {"semantic": 0, "index": 0}


def test_disable_local_embedding_when_torch_broken(monkeypatch):
    from jshi.memory.rems3 import disable_local_embedding_if_needed

    monkeypatch.delenv("JSHI_REMS_SKIP_EMBEDDING", raising=False)
    monkeypatch.setattr(
        "jshi.memory.rems3.embedding_weights_present", lambda *args, **kwargs: True
    )
    monkeypatch.setattr("jshi.memory.rems3.local_torch_usable", lambda: False)
    called = {"semantic": 0}

    class Recall:
        def _semantic_route(self, *args, **kwargs):
            called["semantic"] += 1
            return {"evt": 1}

        def index_event(self, event) -> None:
            return None

    pipeline = SimpleNamespace(
        config=SimpleNamespace(embedding=SimpleNamespace(provider="local", model_name="x")),
        recall_pipeline=Recall(),
    )
    assert disable_local_embedding_if_needed(pipeline) is True
    assert pipeline.recall_pipeline._semantic_route("stone", "你好") == {}
    assert called["semantic"] == 0


def test_adapter_ingest_and_recall_use_engine_shapes():
    pipeline = FakePipeline()
    backend = Rems3MemoryBackend(pipeline)
    result = backend.ingest_batch(_sample_batch())

    assert len(pipeline.ingested) == 1
    engine_batch = pipeline.ingested[0]
    assert not hasattr(engine_batch, "batch_id")
    assert not hasattr(engine_batch, "from_sequence")
    assert engine_batch.experiences[0].text == "你好"
    assert result.sealed_event_ids == ("evt-1", "evt-2")
    assert result.stored_marks["seg-1"] == ("evt-1", "evt-2")

    fragments = backend.recall("stone", "你好", level=2, limit=3, object_id="OBJ-A")
    assert fragments[0].event_type == "memory"
    assert fragments[0].content == "摘要"
    assert pipeline.queries[0][2]["level"] == 2
    assert pipeline.queries[0][2]["limit"] == 3


def test_remember_fact_synthesizes_single_ingest():
    pipeline = FakePipeline()
    backend = Rems3MemoryBackend(pipeline)

    event_id = backend.remember_fact("stone", "external_input", "往事", ("s1",))

    assert event_id == "evt-1"
    assert len(pipeline.ingested) == 1
    assert len(pipeline.ingested[0].experiences) == 1
    assert pipeline.ingested[0].experiences[0].text == "往事"
    assert pipeline.ingested[0].experiences[0].source_ids == ("s1",)


def test_build_memory_backend_defaults_to_inprocess(monkeypatch, tmp_path):
    monkeypatch.delenv("JSHI_MEMORY_BACKEND", raising=False)
    repository = SubjectRepository(tmp_path / "subject.sqlite3")

    backend = build_memory_backend(repository, tmp_path)

    assert type(backend).__name__ == "InProcessMemoryBackend"


def test_build_memory_backend_unknown_name_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("JSHI_MEMORY_BACKEND", "vector-db")
    repository = SubjectRepository(tmp_path / "subject.sqlite3")

    with pytest.raises(UnknownMemoryBackendError, match="vector-db"):
        build_memory_backend(repository, tmp_path)


def test_build_rems3_without_package_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("JSHI_MEMORY_BACKEND", "rems3")

    def boom() -> None:
        raise RemsUnavailableError("need rems")

    monkeypatch.setattr("jshi.memory.rems3.load_rems", boom)
    repository = SubjectRepository(tmp_path / "subject.sqlite3")

    with pytest.raises(RemsUnavailableError, match="rems"):
        build_memory_backend(repository, tmp_path)


def test_rems_data_dir_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("JSHI_REMS_DATA_DIR", raising=False)
    assert rems_data_dir(tmp_path) == tmp_path / "rems"

    monkeypatch.setenv("JSHI_REMS_DATA_DIR", str(tmp_path / "custom"))
    assert rems_data_dir(tmp_path) == tmp_path / "custom"


def test_runtime_defaults_to_inprocess_shell(monkeypatch, tmp_path):
    monkeypatch.delenv("JSHI_MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("JSHI_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("JSHI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_MODEL_NAME", raising=False)

    process, _, _subjects = cli._runtime(tmp_path)

    assert type(process.memory._backend).__name__ == "InProcessMemoryBackend"


def test_openai_compat_base_url_strips_chat_completions():
    assert (
        openai_compat_base_url("https://api.deepseek.com/v1/chat/completions")
        == "https://api.deepseek.com/v1"
    )
    assert openai_compat_base_url("https://api.deepseek.com") == "https://api.deepseek.com"


def test_apply_jshi_llm_fills_empty_rems_key(monkeypatch):
    monkeypatch.setenv("JSHI_MODEL_API_KEY", "sk-test")
    monkeypatch.setenv(
        "JSHI_MODEL_ENDPOINT", "https://api.deepseek.com/v1/chat/completions"
    )
    monkeypatch.setenv("JSHI_MODEL_NAME", "deepseek-v4-flash")
    config = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="",
            base_url="https://api.deepseek.com",
            task_models=SimpleNamespace(default="old"),
        )
    )

    apply_jshi_llm_settings(config)

    assert config.llm.api_key == "sk-test"
    assert config.llm.base_url == "https://api.deepseek.com/v1"
    assert config.llm.task_models.default == "deepseek-v4-flash"


def test_apply_jshi_llm_keeps_explicit_rems_key(monkeypatch):
    monkeypatch.setenv("JSHI_MODEL_API_KEY", "sk-jshi")
    config = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="sk-rems",
            base_url="https://api.deepseek.com",
            task_models=SimpleNamespace(default="x"),
        )
    )

    apply_jshi_llm_settings(config)

    assert config.llm.api_key == "sk-rems"


def test_build_rems_pipeline_uses_data_dir(tmp_path):
    class _Storage:
        database_url = ""
        qdrant_path = None

    class _Cfg:
        def __init__(self) -> None:
            self.storage = _Storage()

    class _Pipe:
        @classmethod
        def from_config(cls, config):
            return SimpleNamespace(config=config)

    data_dir = tmp_path / "rems"
    pipe = build_rems_pipeline(data_dir, _Pipe, _Cfg)

    assert data_dir.is_dir()
    assert pipe.config.storage.database_url.endswith("/rems.db")
    assert Path(pipe.config.storage.qdrant_path) == (data_dir / "qdrant").resolve()


def test_build_rems_pipeline_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("JSHI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_MODEL_ENDPOINT", raising=False)

    class _Storage:
        database_url = ""
        qdrant_path = None

    class _Cfg:
        def __init__(self) -> None:
            self.storage = _Storage()
            self.llm = SimpleNamespace(api_key="", base_url="", task_models=None)

    class _Pipe:
        @classmethod
        def from_config(cls, config):
            return config

    with pytest.raises(RemsUnavailableError, match="JSHI_MODEL_API_KEY"):
        build_rems_pipeline(tmp_path / "rems", _Pipe, _Cfg)


def test_quiet_embedding_logs_sets_transformers_env(monkeypatch) -> None:
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    monkeypatch.delenv("TRANSFORMERS_VERBOSITY", raising=False)
    monkeypatch.delenv("TQDM_DISABLE", raising=False)
    quiet_embedding_logs()
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert os.environ["TRANSFORMERS_VERBOSITY"] == "error"
    assert os.environ["TQDM_DISABLE"] == "1"


@pytest.mark.rems
@pytest.mark.skipif(os.getenv("JSHI_TEST_REMS") != "1", reason="set JSHI_TEST_REMS=1 to run live rems")
def test_live_rems_ingest_and_recall(tmp_path):
    pytest.importorskip("rems")

    backend = Rems3MemoryBackend(data_dir=tmp_path / "rems")
    result = backend.ingest_batch(_sample_batch())
    assert result.subject_id == "stone"
    backend.recall("stone", "你好", level=1, limit=1)
