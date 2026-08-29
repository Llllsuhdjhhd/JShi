# REMS3 并发要求（供 Jshi_memory 侧改造）

## 背景

JShi 已把「30 投递」改成异步：`REMSPipeline.ingest_batch` 放到后台线程执行，活动不再等记忆；
下一轮活动的 `recall` 仍在主线程同步执行。

因此，**同一个 `REMSPipeline` 实例会被两个线程并发调用**：

- 后台线程：`Rems3MemoryBackend.ingest_batch` → `REMSPipeline.ingest_batch`
- 主线程：`Rems3MemoryBackend.recall` → `REMSPipeline.recall`

## 目标

1. **recall 不被 ingest 阻塞**：ingest 进行中，recall 应能立即/正常返回。
2. **不互相破坏状态**：ingest 的写与 recall 的读不能导致异常或数据损坏。
3. 允许「最终一致」：recall 允许读不到 ingest 尚未封存完成的最新内容，但不允许等待、锁死或报错。

## JShi 侧已保证的前提

- 同一时间**只有一个 ingest 批次在跑**（`InProcessMemoryControl` 用 `previous_status` 门禁，上一批未结束不会触发新一批）。
- 所以 Jshi_memory **不需要处理 ingest 与 ingest 的并发**，只需要处理 **ingest 与 recall 的并发**。

## 需要确认/处理的并发点（Jshi_memory 侧）

1. **共享嵌入模型 `SentenceTransformerEmbedding`**
   - `ingest_batch` 里的 `index_event` 会调 `embed_documents`；
   - `recall` 会调 `embed_query`；
   - 两者共用同一个 `self._model`（`SentenceTransformer` 实例）。
   - 需要确认并发 `encode` 是否线程安全；不安全时给嵌入加锁，或为 read/write 使用独立模型实例。
   - 只允许串行「嵌入」这个窄操作，**不允许把整个 ingest/recall 串起来**。

2. **SQLite（rems.db）**
   - `Database.session()` 每次新建 SQLAlchemy session，但底层是单文件 SQLite。
   - ingest 有多次写事务（events / roles / object_memory_entries / stored_marks 等），recall 有多次读。
   - 建议：
     - 开启 WAL 模式，让写不阻塞读；
     - 设置合理的 `busy_timeout`，避免 `database is locked`；
     - 确认各 repo 都是「每操作独立 session」，无跨线程共享连接。

3. **Qdrant 本地向量库**
   - ingest 写向量（`index_event`），recall 搜索向量。
   - 确认 `QdrantClient(path=...)` 本地模式并发读写安全；不安全时对向量库访问做窄锁或读写分离。

4. **领域服务与 Repository 的跨线程共享状态**
   - 检查 `MetabolismService`、`EventService`、`RoleService`、各 `*Repository` 是否持有跨线程共享的可变状态（缓存、计数器、pending 缓冲等）。
   - 若有，需要加锁或改成线程局部/每会话独立。

## 验收标准

- 写一个并发测试：用一个**慢/阻塞的 ingest** 与一个并发的 `recall` 同时跑，断言：
  - `recall` 在 ingest 进行中正常返回；
  - 不出现 `database is locked`、嵌入模型异常、向量库异常；
  - 最终 ingest 完成且 `stored_marks`/游标推进正确。
- 允许的折中：ingest 与 ingest 串行；但 **recall 必须始终独立、不被 ingest 锁住**。

## 对应入口

- JShi 侧适配器：`src/jshi/memory/rems3.py` 的 `Rems3MemoryBackend.ingest_batch` / `recall`
- Jshi_memory 侧：`rems/pipeline.py` 的 `REMSPipeline.ingest_batch` / `recall`
