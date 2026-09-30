"""人物长期经验与事件回忆保持独立，并进入当前对话对象的上下文。"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

from jshi.assembly import AssemblyContext, AssemblySpeaker, PersonExperienceSource, PersonPortraitSource
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.longtermexperience import PersonExperience, RemsLongTermExperience
from jshi.memory import InProcessMemoryBackend, MemoryShell
from jshi.models import ModelRequest, ModelResponse
from jshi.models.prompt import build_user
from jshi.recognition import ObjectProfile
from jshi.subject import SubjectProcess, SubjectRepository


class _ExperiencePort:
    def __init__(self) -> None:
        self.calls = []

    def recall_experience(self, subject_id, query, *, object_ids, budget_chars=500):
        self.calls.append((subject_id, query, object_ids, budget_chars))
        return (
            PersonExperience(
                object_id="OBJ-USER",
                content="谈事务时先说清要点。",
                variant="full",
                source_event_ids=("EVT-1", "EVT-2"),
            ),
        )

    def refresh_person_experience(self, subject_id, object_id):
        raise AssertionError("对话装配不得触发经验维护")


def test_source_only_recalls_confirmed_interlocutor() -> None:
    port = _ExperiencePort()
    source = PersonExperienceSource(port)
    provisional = source.load(
        AssemblyContext(
            subject_id="stone",
            input_text="你好",
            speaker=AssemblySpeaker("OBJ-USER", "用户", status="provisional"),
        )
    )
    assert provisional.fragments == ()
    confirmed = source.load(
        AssemblyContext(
            subject_id="stone",
            input_text="有件事想商量",
            speaker=AssemblySpeaker("OBJ-USER", "用户", status="confirmed"),
        )
    )
    assert port.calls == [("stone", "有件事想商量", ("OBJ-USER",), 500)]
    fragment = confirmed.fragments[0]
    assert fragment.kind == "person_experience"
    assert fragment.source_ids == ("EVT-1", "EVT-2")
    assert fragment.variant == "full"


def test_portrait_source_uses_confirmed_speaker_and_bounded_level() -> None:
    class _Memory:
        def __init__(self):
            self.calls = []

        def portrait(self, subject_id, object_id):
            self.calls.append((subject_id, object_id))
            return {
                "subject_id": subject_id,
                "object_id": object_id,
                "levels": {"L1": "甲" * 500, "L2": "许澄谨慎核对证据。"},
            }

    memory = _Memory()
    source = PersonPortraitSource(memory, budget_chars=30)
    ctx = AssemblyContext("stone", "你好", AssemblySpeaker("OBJ-USER", "许澄", status="provisional"))
    assert source.load(ctx).fragments == ()
    assert memory.calls == []
    ctx = AssemblyContext("stone", "你好", AssemblySpeaker("OBJ-USER", "许澄", status="confirmed"))
    fragment = source.load(ctx).fragments[0]
    assert fragment.source == "person_portrait"
    assert fragment.content == "许澄谨慎核对证据。"
    assert memory.calls == [("stone", "OBJ-USER")]


def test_rems_adapter_maps_person_result_without_event_recall() -> None:
    class _Module:
        def recall(self, query):
            assert query.object_ids == ("OBJ-USER",)
            assert query.general_limit == 0
            return SimpleNamespace(
                person=(
                    SimpleNamespace(
                        object_id="OBJ-USER",
                        content="谈事务时先说清要点。",
                        variant="brief",
                        source_event_ids=("EVT-1",),
                        updated_at=datetime(2026, 9, 27),
                        score=0.9,
                    ),
                )
            )

    adapter = RemsLongTermExperience(_Module(), lambda **kwargs: SimpleNamespace(**kwargs))
    hits = adapter.recall_experience(
        "stone", "怎样沟通", object_ids=("OBJ-USER",), budget_chars=120
    )
    assert len(hits) == 1
    assert hits[0].variant == "brief"
    assert hits[0].source_event_ids == ("EVT-1",)


def test_process_passes_person_experience_to_cognition(tmp_path) -> None:
    class _CaptureModel:
        name = "capture"

        def __init__(self) -> None:
            self.request = None

        def generate(self, request: ModelRequest) -> ModelResponse:
            self.request = request
            return ModelResponse(text="知道了", model=self.name)

    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    model = _CaptureModel()
    port = _ExperiencePort()
    memory = MemoryShell(InProcessMemoryBackend(repository))
    memory.portrait = lambda subject_id, object_id: {
        "subject_id": subject_id,
        "object_id": object_id,
        "levels": {"L1": "许澄在核对展览证据时谨慎。"},
    }
    process = SubjectProcess(
        repository, identities, model, memory=memory, long_term_experience=port
    )
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="用户", source="test", status="confirmed"
        )
    )
    turn = process.experience("stone", "有件事想商量", object_ref="用户")
    assert any(f.source == "person_experience" for f in turn.current_state.fragments)
    assert any(f.source == "person_portrait" for f in turn.current_state.fragments)
    assert model.request is not None
    context = [item for item in model.request.context if item.get("source") == "person_experience"]
    assert context[0]["source_event_ids"] == ("EVT-1", "EVT-2")
    prompt = build_user(model.request)
    assert "【人物相处经验·可修订】" in prompt
    assert "谈事务时先说清要点" in prompt
    assert "【人物肖像·可修订】" in prompt
    assert "许澄在核对展览证据时谨慎" in prompt


def test_rems_ingest_schedules_refresh_after_return(tmp_path) -> None:
    from jshi.memory import MemoryBatch, MemoryExperience, Rems3MemoryBackend

    class _Pipeline:
        def ingest_batch(self, batch):
            return SimpleNamespace(
                subject_id=batch.subject_id,
                stored_marks={},
                sealed_event_ids=(),
                role_ids=("OBJ-USER",),
                unclosed_count=0,
                errors=(),
            )

    class _Refresh:
        def __init__(self) -> None:
            self.called = threading.Event()
            self.items = []

        def refresh_person_experience(self, subject_id, object_id):
            self.items.append((subject_id, object_id))
            self.called.set()

    class _PortraitRefresh:
        def __init__(self) -> None:
            self.items = []

        def refresh(self, subject_id, object_id):
            self.items.append((subject_id, object_id))

    backend = Rems3MemoryBackend(_Pipeline())
    refresh = _Refresh()
    portrait = _PortraitRefresh()
    backend.long_term_experience = refresh
    backend._portrait_refresh = portrait
    backend.ingest_batch(
        MemoryBatch(
            batch_id="batch-1",
            subject_id="stone",
            experiences=(MemoryExperience(subject_id="stone", text="你好"),),
            from_sequence=1,
            to_sequence=1,
        )
    )
    assert refresh.called.wait(2)
    backend._experience_refresh_queue.join()
    assert refresh.items == [("stone", "OBJ-USER")]
    assert portrait.items == [("stone", "OBJ-USER")]


def test_refresh_worker_is_daemon_and_exit_wait_is_bounded(tmp_path) -> None:
    import time

    from jshi.memory import MemoryBatch, MemoryExperience, Rems3MemoryBackend

    class _Pipeline:
        def ingest_batch(self, batch):
            return SimpleNamespace(
                subject_id=batch.subject_id, stored_marks={}, sealed_event_ids=(),
                role_ids=("OBJ-A", "OBJ-B"), unclosed_count=0, errors=(),
            )

    release = threading.Event()
    started = threading.Event()

    class _SlowRefresh:
        def __init__(self) -> None:
            self.items = []

        def refresh_person_experience(self, subject_id, object_id):
            self.items.append(object_id)
            started.set()
            release.wait(5)

    backend = Rems3MemoryBackend(_Pipeline())
    refresh = _SlowRefresh()
    backend.long_term_experience = refresh
    backend._experience_exit_wait_s = 0.2
    backend.ingest_batch(
        MemoryBatch(
            batch_id="batch-1", subject_id="stone",
            experiences=(MemoryExperience(subject_id="stone", text="你好"),),
            from_sequence=1, to_sequence=1,
        )
    )
    assert started.wait(2)
    worker = backend._experience_refresh_worker
    assert worker is not None and worker.daemon
    began = time.monotonic()
    backend._wait_experience_refresh_on_exit()
    assert time.monotonic() - began < 2
    release.set()
    worker.join(2)
    assert refresh.items == ["OBJ-A"]
