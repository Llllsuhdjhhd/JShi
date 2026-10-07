"""Jshi_memory（REMS3）同进程适配器：只做形状互转，不引进引擎类型到主路径。"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import warnings
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from .contracts import (
    BackendIngestResult,
    MemoryBatch,
    MemoryExperience,
    new_id,
)
from .port import RecalledFragment

logger = logging.getLogger("jshi.memory")

_DEFAULT_EVENT_TYPE = "memory"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class RemsUnavailableError(RuntimeError):
    """``JSHI_MEMORY_BACKEND=rems3`` 但 ``rems`` 包不可导入。"""


def neighbor_rems_src() -> Path | None:
    """邻仓 ``../Jshi_memory/src``（与本仓库并列）。没有 port.py 则不算。"""
    repo = Path(__file__).resolve().parents[3]
    neighbor = repo.parent / "Jshi_memory" / "src"
    if (neighbor / "rems" / "port.py").is_file():
        return neighbor
    return None


def prefer_neighbor_rems() -> Path | None:
    """本机 conda 可能先导入另一份旧 ``rems``（无 ``port``）。邻仓放到路径最前。"""
    neighbor = neighbor_rems_src()
    if neighbor is None:
        return None
    path = str(neighbor)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    existing = sys.modules.get("rems")
    if existing is not None:
        file = (getattr(existing, "__file__", "") or "").replace("\\", "/")
        if "Jshi_memory" not in file:
            for name in list(sys.modules):
                if name == "rems" or name.startswith("rems."):
                    del sys.modules[name]
    return neighbor


def quiet_embedding_logs() -> None:
    """句向量加载会往 stderr 打进度条、LOAD REPORT、flash attention 警告；对话里不需要。"""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TQDM_DISABLE", "1")
    logging.getLogger("transformers").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    warnings.filterwarnings(
        "ignore",
        message=".*flash attention.*",
        category=UserWarning,
    )


def load_rems() -> tuple[Any, Any, Any]:
    """懒加载邻仓包。失败时不退回进程内后端。"""
    quiet_embedding_logs()
    prefer_neighbor_rems()
    try:
        from rems import config as rems_config
        from rems import pipeline as rems_pipeline
        from rems import port as rems_port
    except ImportError as exc:
        imported = ""
        module = sys.modules.get("rems")
        if module is not None:
            imported = getattr(module, "__file__", "") or ""
        raise RemsUnavailableError(
            "JSHI_MEMORY_BACKEND=rems3 需要当前这条 python 能导入邻仓 Jshi_memory 的 rems.port。"
            f" 当前解释器：{sys.executable}"
            + (f" 当前导入到的 rems：{imported}" if imported else "")
            + f' 请确认 {sys.executable} 能找到 ../Jshi_memory/src，或对该解释器执行 pip install -e <Jshi_memory>'
        ) from exc
    return rems_port, rems_pipeline.REMSPipeline, rems_config.REMSConfig


def experience_payload(experience: MemoryExperience) -> dict[str, Any]:
    """经历 → REMS 可接受的字段（不含 sub_segments）。"""

    return {
        "subject_id": experience.subject_id,
        "text": experience.text,
        "objects": dict(experience.objects or {}),
        "interlocutor": getattr(experience, "interlocutor", None),
        "source_ids": tuple(experience.source_ids or ()),
        "occurred_at": _local_naive(experience.occurred_at),
        "segment_id": experience.segment_id,
        "origin": experience.origin,
    }


def _local_naive(value: datetime | None) -> datetime | None:
    """REMS 用本地无时区墙钟；有时区的先转到本地再去掉 tzinfo。"""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def batch_payload(batch: MemoryBatch) -> dict[str, Any]:
    """丢掉 30 台账字段，只留契约输入：subject_id + experiences。"""

    return {
        "subject_id": batch.subject_id,
        "experiences": tuple(experience_payload(item) for item in batch.experiences),
    }


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def from_ingest_result(raw: Any) -> BackendIngestResult:
    stored_raw = getattr(raw, "stored_marks", None) or {}
    stored_marks = {
        str(key): _as_tuple(ids) for key, ids in dict(stored_raw).items()
    }
    return BackendIngestResult(
        subject_id=raw.subject_id,
        stored_marks=stored_marks,
        sealed_event_ids=_as_tuple(getattr(raw, "sealed_event_ids", ())),
        role_ids=_as_tuple(getattr(raw, "role_ids", ())),
        unclosed_count=int(getattr(raw, "unclosed_count", 0) or 0),
        errors=_as_tuple(getattr(raw, "errors", ())),
    )


def from_recalled_fragments(raw: Sequence[Any] | None) -> tuple[RecalledFragment, ...]:
    fragments: list[RecalledFragment] = []
    for item in raw or ():
        event_type = getattr(item, "event_type", None) or _DEFAULT_EVENT_TYPE
        kind = getattr(item, "kind", None) or "fact"
        occurred_at = getattr(item, "occurred_at", None)
        if isinstance(occurred_at, str):
            try:
                occurred_at = datetime.fromisoformat(occurred_at)
            except ValueError:
                occurred_at = None
        if isinstance(occurred_at, datetime) and occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
        fragments.append(
            RecalledFragment(
                event_id=item.event_id,
                event_type=event_type,
                text=item.text,
                content=getattr(item, "content", None) or "",
                summary_level=getattr(item, "summary_level", None),
                kind=kind,
                object_id=getattr(item, "object_id", None),
                interlocutor=getattr(item, "interlocutor", None),
                interlocutor_object_ids=_as_tuple(getattr(item, "interlocutor_object_ids", ())),
                source_ids=_as_tuple(getattr(item, "source_ids", ())),
                score=float(getattr(item, "score", 0.0) or 0.0),
                occurred_at=occurred_at,
            )
        )
    return tuple(fragments)


def openai_compat_base_url(endpoint: str) -> str:
    """把匠石的完整 completions 地址收成 OpenAI 客户端的 base_url。"""

    text = endpoint.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text or endpoint.strip()


def apply_jshi_llm_settings(config: Any) -> None:
    """REMS 密钥为空时，沿用匠石已有的 JSHI_MODEL_*。已写 REMS_LLM__* 的不覆盖。"""

    llm = getattr(config, "llm", None)
    if llm is None:
        return
    if not (getattr(llm, "api_key", None) or "").strip():
        key = (os.getenv("JSHI_MODEL_API_KEY") or "").strip()
        if key:
            llm.api_key = key
    endpoint = (os.getenv("JSHI_MODEL_ENDPOINT") or "").strip()
    if endpoint:
        llm.base_url = openai_compat_base_url(endpoint)
    name = (os.getenv("JSHI_MODEL_NAME") or "").strip()
    mapping = getattr(llm, "task_models", None)
    if name and mapping is not None:
        mapping.default = name


def _sqlite_url(db_path: Path) -> str:
    return "sqlite:///" + db_path.resolve().as_posix()


_WEIGHT_SUFFIXES = {".safetensors", ".bin", ".pt", ".onnx"}


def embedding_weights_present(
    model_name: str, cache_root: Path | None = None
) -> bool:
    """只看磁盘上有没有权重大于 1MB，不 import torch。"""
    name = (model_name or "").strip()
    if not name:
        return False
    root = cache_root or (Path.home() / ".cache" / "huggingface" / "hub")
    folder = root / ("models--" + name.replace("/", "--"))
    if not folder.is_dir():
        return False
    for path in folder.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in _WEIGHT_SUFFIXES
            and path.stat().st_size > 1_000_000
        ):
            return True
    return False


def local_torch_usable() -> bool:
    """当前解释器能否真正加载 torch。base 环境的 c10.dll 会 WinError 1114。"""
    try:
        import torch

        torch.zeros(1)
    except Exception:
        return False
    return True


def disable_local_embedding_if_needed(pipeline: Any) -> bool:
    """权重缺失、torch 不可用或显式跳过时，关掉语义向量，避免卡住对话。

    不改成 hash 嵌入：维度不同会让 Qdrant 删掉已有 collection。
    """
    if os.getenv("JSHI_REMS_SKIP_EMBEDDING", "").strip() in {"1", "true", "yes"}:
        skip = True
        reason = "JSHI_REMS_SKIP_EMBEDDING"
    else:
        config = getattr(pipeline, "config", None)
        embedding = getattr(config, "embedding", None) if config is not None else None
        provider = str(getattr(embedding, "provider", "") or "local")
        if provider == "hash":
            return False
        model_name = str(getattr(embedding, "model_name", "") or "")
        if not embedding_weights_present(model_name):
            skip = True
            reason = f"本地没有 {model_name or 'embedding'} 权重"
        elif not local_torch_usable():
            skip = True
            reason = f"当前 python 的 torch 无法加载（{sys.executable}）"
        else:
            skip = False
            reason = ""
    if not skip:
        return False
    recall_pipeline = getattr(pipeline, "recall_pipeline", None)
    if recall_pipeline is None:
        return False
    recall_pipeline._semantic_route = lambda *args, **kwargs: {}
    recall_pipeline.index_event = lambda event: None
    message = (
        f"[jshi] {reason}，已跳过语义召回（不加载 torch）。"
        "词法/对象召回仍可用。修好 torch 并下载 BAAI/bge-small-zh-v1.5 后重启。"
    )
    logger.warning(message)
    print(message, file=sys.stderr)
    return True


def build_rems_pipeline(data_dir: Path, pipeline_cls: Any, config_cls: Any) -> Any:
    data_dir.mkdir(parents=True, exist_ok=True)
    config = config_cls()
    apply_jshi_llm_settings(config)
    if hasattr(config, 'portrait_max_chars'):
        config.portrait_max_chars = 800
        config.portrait_max_levels = 3
    llm = getattr(config, "llm", None)
    if llm is not None and not (getattr(llm, "api_key", None) or "").strip():
        raise RemsUnavailableError(
            "rems3 启动需要模型密钥：在仓库根 .env 写 JSHI_MODEL_API_KEY，"
            "或写 REMS_LLM__API_KEY（OpenAI 兼容客户端不允许空密钥）"
        )
    config.storage.database_url = _sqlite_url(data_dir / "rems.db")
    # 本地路径模式，不必 Docker；与 sqlite 同落在 {data_dir}/rems/
    config.storage.qdrant_path = str((data_dir / "qdrant").resolve())
    return pipeline_cls.from_config(config)


class Rems3MemoryBackend:
    """把本仓 dataclass 端口接到 ``REMSPipeline``（同进程，不启 HTTP）。"""

    def __init__(
        self,
        pipeline: Any | None = None,
        *,
        data_dir: Path | None = None,
        engine_types: tuple[Any, Any] | None = None,
    ) -> None:
        if pipeline is not None:
            self._pipeline = pipeline
            self._engine_types = engine_types
        else:
            rems_port, pipeline_cls, config_cls = load_rems()
            self._engine_types = (rems_port.MemoryBatch, rems_port.MemoryExperience)
            self._pipeline = build_rems_pipeline(
                data_dir or Path(".jshi") / "rems",
                pipeline_cls,
                config_cls,
            )
        disable_local_embedding_if_needed(self._pipeline)
        self.long_term_experience = None
        self._portrait_refresh = None
        from .portrait_views import PortraitViews
        self._portrait_views = PortraitViews(Path(data_dir or '.jshi/rems') / 'jshi_person_views.json')
        if all(
            hasattr(self._pipeline, name)
            for name in ("config", "role_repo", "db", "llm")
        ):
            from jshi.longtermexperience import RemsLongTermExperience

            self.long_term_experience = RemsLongTermExperience.from_pipeline(
                self._pipeline
            )
            if getattr(self._pipeline, "portrait_service", None) is not None:
                from rems.services.white_painting_portrait import WhitePaintingPortraitBuilder
                from rems.storage.repository import PortraitRepository

                self._portrait_refresh = WhitePaintingPortraitBuilder(
                    self._pipeline.config,
                    self._pipeline.role_repo,
                    PortraitRepository(self._pipeline.db),
                    self._pipeline.llm,
                )
        self._experience_refresh_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._experience_refresh_lock = threading.Lock()
        self._experience_refresh_queued: set[tuple[str, str]] = set()
        self._experience_refresh_worker: threading.Thread | None = None
        self._experience_refresh_stopping = False
        self._experience_exit_hook = False
        self._experience_exit_wait_s = _env_float("JSHI_EXPERIENCE_EXIT_WAIT_S", 30.0)

    def _to_engine_batch(self, payload: Mapping[str, Any]) -> Any:
        experiences_raw = payload["experiences"]
        if self._engine_types is None:
            experiences = tuple(
                SimpleNamespace(**dict(item)) for item in experiences_raw
            )
            return SimpleNamespace(
                subject_id=payload["subject_id"],
                experiences=experiences,
            )
        batch_cls, experience_cls = self._engine_types
        experiences = tuple(experience_cls(**dict(item)) for item in experiences_raw)
        return batch_cls(
            subject_id=payload["subject_id"],
            experiences=experiences,
        )

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult:
        payload = batch_payload(batch)
        engine_batch = self._to_engine_batch(payload)
        raw = self._pipeline.ingest_batch(engine_batch)
        result = from_ingest_result(raw)
        for object_id in result.role_ids:
            self._schedule_experience_refresh(batch.subject_id, object_id)
        return result

    def _schedule_experience_refresh(self, subject_id: str, object_id: str) -> None:
        if (self.long_term_experience is None and self._portrait_refresh is None) or not subject_id or not object_id:
            return
        key = (subject_id, object_id)
        with self._experience_refresh_lock:
            if key in self._experience_refresh_queued:
                return
            self._experience_refresh_queued.add(key)
            self._experience_refresh_queue.put(key)
            worker = self._experience_refresh_worker
            if worker is None or not worker.is_alive():
                # 守护线程不拖住退出；退出时限时等待当前这一项做完。
                # 没做完的对象不会丢：待整理白描仍在库里，下次该人入库时一并吸收。
                worker = threading.Thread(
                    target=self._refresh_experience_loop,
                    name="jshi-person-experience",
                    daemon=True,
                )
                self._experience_refresh_worker = worker
                if not self._experience_exit_hook:
                    atexit.register(self._wait_experience_refresh_on_exit)
                    self._experience_exit_hook = True
                worker.start()

    def _wait_experience_refresh_on_exit(self) -> None:
        self._experience_refresh_stopping = True
        worker = self._experience_refresh_worker
        if worker is None or not worker.is_alive():
            return
        worker.join(self._experience_exit_wait_s)
        if worker.is_alive():
            logger.warning(
                "退出时人物经验维护未在 %.0f 秒内完成，剩余部分留待下次入库",
                self._experience_exit_wait_s,
            )

    def _refresh_experience_loop(self) -> None:
        while True:
            with self._experience_refresh_lock:
                if self._experience_refresh_stopping:
                    self._experience_refresh_worker = None
                    return
                try:
                    subject_id, object_id = self._experience_refresh_queue.get_nowait()
                except queue.Empty:
                    self._experience_refresh_worker = None
                    return
                self._experience_refresh_queued.discard((subject_id, object_id))
            try:
                if self.long_term_experience is not None:
                    self.long_term_experience.refresh_person_experience(
                        subject_id, object_id
                    )
            except Exception:
                logger.exception("人物经验维护失败：%s/%s", subject_id, object_id)
            try:
                if self._portrait_refresh is not None:
                    self._portrait_refresh.refresh(subject_id, object_id)
                self._portrait_views.refresh(self._pipeline.portrait(subject_id, object_id), getattr(self._pipeline, 'llm', None))
            except Exception:
                logger.exception("人物描述肖像维护失败：%s/%s", subject_id, object_id)
            finally:
                self._experience_refresh_queue.task_done()

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        object_ids: tuple[str, ...] = (),
        interlocutor_object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]:
        raw = self._pipeline.recall(
            subject_id,
            query,
            object_id=object_id,
            object_ids=tuple(dict.fromkeys((
                *((interlocutor_object_id,) if interlocutor_object_id else ()),
                *object_ids,
                *((object_id,) if object_id else ()),
            ))),
            level=level,
            limit=limit,
            anchor_event_ids=anchor_event_ids,
        )
        fragments = from_recalled_fragments(raw)
        if interlocutor_object_id:
            # 只在相关度接近时把本轮对话人关联结果前移；明显更相关的其他对象不被压过。
            close = float(getattr(self._pipeline, "_relevance_close", 0.0) or 0.0)
            if close > 0 and len(fragments) > 1:
                highest = max(item.score for item in fragments)
                fragments = tuple(sorted(
                    fragments,
                    key=lambda item: (
                        0 if highest - item.score <= close and item.object_id == interlocutor_object_id else 1,
                    ),
                ))
        return tuple(
            replace(item, query_object_role=(
                "interlocutor" if interlocutor_object_id and item.object_id == interlocutor_object_id
                else "involved" if item.object_id in object_ids or item.object_id == object_id
                else None
            ))
            for item in fragments
        )

    def remember_fact(
        self,
        subject_id: str,
        event_type: str,
        text: str,
        source_ids: tuple[str, ...] = (),
    ) -> str:
        del event_type  # REMS 端口无此字段；测试播种走单条 ingest
        batch = MemoryBatch(
            batch_id=new_id(),
            subject_id=subject_id,
            experiences=(
                MemoryExperience(
                    subject_id=subject_id,
                    text=text,
                    source_ids=source_ids,
                    origin="external",
                ),
            ),
            from_sequence=0,
            to_sequence=0,
        )
        result = self.ingest_batch(batch)
        if result.sealed_event_ids:
            return result.sealed_event_ids[0]
        return ""

    def portrait(self, subject_id: str, object_id: str) -> dict | None:
        """对象人物肖像(REMS 特性)。同进程 REMSPipeline 已实现 ``portrait``。"""
        raw = self._pipeline.portrait(subject_id, object_id)
        if not raw:
            return raw
        view, ready = self._portrait_views.get(raw)
        if not ready:
            self._schedule_experience_refresh(subject_id, object_id)
        return view

    def assemble_recall_block(
        self,
        subject_id: str,
        query: str,
        *,
        object_id: str | None = None,
        level: int = 1,
        limit: int | None = None,
        anchor_event_ids: tuple[str, ...] = (),
        budget: int | None = None,
    ) -> dict:
        """召回块(REMS 特性):预算受限的条目 + 对话人肖像并入。透传给 REMSPipeline。"""
        return self._pipeline.assemble_recall_block(
            subject_id,
            query,
            object_id=object_id,
            level=level,
            limit=limit,
            anchor_event_ids=anchor_event_ids,
            budget=budget,
        )
