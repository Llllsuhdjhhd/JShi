# 给 Jshi_memory 的记忆交接说明

> **读者**：邻仓 `Jshi_memory`（REMS3 fork，包名 `rems`）维护者。  
> **目的**：单份说清本仓（JShi / 匠石）如何使用记忆、端口长什么样、两边各管什么、已知债务与近期协作方向。  
> **权威契约**：仍以本仓 [`doc/design/记忆层契约.md`](design/记忆层契约.md) 为准；本文是面向实施的汇总，冲突时以契约 + 现码为准。  
> **编写日期**：2026-09-20（对应本仓分支意图：`memory/cross-session-continuity`）。

---

## 0. 一句话

- **JShi**：经历账本（16）→ 冲刷编批（30）→ 薄壳端口（09）→ 组装时召回（03）；策略快照与召回流水留在匠石侧。  
- **Jshi_memory**：实现 `MemoryBackendPort`（代谢、封存、向量、遗忘、摘要档位等）；**不建对象、不改活跃区、不写 `subject.sqlite3`**。  
- 两边类型故意不合并：JShi = dataclass；REMS = pydantic；互转只在本仓 `Rems3MemoryBackend`。

邻仓路径（本机常见）：`C:\Users\40575\Desktop\prog\Jshi_memory`。  
邻仓对照：`FORK_NOTES.md` 第五节；引擎设计编号见邻仓 `210/220/410/610/810/1010`。

---

## 1. 在主体过程中的位置

主流程（外部活动）七阶段里，记忆只出现在这些点：

```text
① 落位 + 16 追加经历段（原文 text_raw + objects）
② 读活跃区（不碰记忆）
③ 03 组装：MemorySource → recall（只读装载）
④ 建活动
⑤ 05 认知：不调 recall / ingest；可写 memory_ratings（供有效性分析）
⑥ 行动 / 写场
⑦ 07 收尾 → 30 run_async → ingest_batch（后台）
```

要点：

- **事件只在记忆引擎内部**；主链路（02/03/05/07）不使用「事件」概念。  
- **活跃区 ≠ 记忆**：活跃区是工作现场；记忆是可替换后端里的长期材料。  
- **个人世界（08/100）≠ 记忆**：价值、承诺、边界在 08；可核查经历在记忆。  
- **自省（300）≠ 记忆过程**：可调用 `recall` 拼现场；**当前故意不** `append_internal`，故自省正文**不会**经 30 投进记忆。  
- **有效性分析（400）**：旁路；可调回忆策略档位，**不写记忆库**。

---

## 2. 必读文档索引（本仓）

| 路径 | 用途 |
|------|------|
| [`doc/design/记忆层契约.md`](design/记忆层契约.md) | **双边权威**：字段、硬约束、所有权、表示层差异 |
| [`doc/design/09-记忆系统.md`](design/09-记忆系统.md) | 09 薄壳职责、档位、策略、emotion 字段表（设计） |
| [`doc/design/30-记忆数据管控.md`](design/30-记忆数据管控.md) | 冲刷策略 220、批次、游标 |
| [`doc/design/16-经历账本.md`](design/16-经历账本.md) | 原文流、`objects`、活跃区 |
| [`doc/design/00-流程总览与输入边界.md`](design/00-流程总览与输入边界.md) | 阶段③/⑦ 与边界 |
| [`doc/design/03-状态组装.md`](design/03-状态组装.md) | 如何消费 `recall` |
| [`doc/design/05-认知.md`](design/05-认知.md) | 不同轮补召回；不直接调记忆写 |
| [`doc/design/有效性分析系统.md`](design/有效性分析系统.md) | 记忆质量 → 策略快照 |
| [`doc/design/300-自省.md`](design/300-自省.md) | 可回溯 09；现不入 09 |
| [`doc/design/rems3-并发要求.md`](design/rems3-并发要求.md) | **给邻仓**：ingest∥recall 同实例并发 |
| [`doc/rems3-参考笔记.md`](rems3-参考笔记.md) | 旧概念映射；抽象事件一律不做 |
| [`方案(coding)/09-rems3-适配器.md`](../方案(coding)/09-rems3-适配器.md) | 适配器已确认并编码 |
| [`方案(coding)/16-30-回忆对象绑定.md`](../方案(coding)/16-30-回忆对象绑定.md) | objects / interlocutor / 对话行 |

---

## 3. 端口与数据结构（现码）

### 3.1 `MemoryBackendPort`

落点：`src/jshi/memory/backend.py`（邻仓应对齐 `src/rems/port.py`）。

```text
remember_fact(subject_id, event_type, text, source_ids=()) -> str
ingest_batch(batch: MemoryBatch) -> BackendIngestResult
recall(subject_id, query, *, limit=None, object_id=None, level=1, anchor_event_ids=())
    -> Sequence[RecalledFragment]
portrait(subject_id, object_id) -> dict | None   # REMS 特性；进程内恒 None
```

### 3.2 写入：`MemoryExperience` / `MemoryBatch` / `BackendIngestResult`

落点：`src/jshi/memory/contracts.py`。

```text
MemoryExperience
  subject_id: str
  text: str                    # 30 编成的对话行（含说话人名字）；可空
  objects: Mapping[str, str]   # 名字/称呼 → 01 object_id；空 = 主体记忆
  source_ids: tuple[str, ...]
  occurred_at: datetime | None
  segment_id: str | None       # 经历段 id → stored_marks 键
  origin: "external" | "internal" | "dream"
  interlocutor: str | None     # 本段对话对象 object_id；自述/系统可空
                               # （契约正文早期稿可能未写；现码与 16-30 绑定已带）

MemoryBatch
  batch_id, subject_id, experiences
  from_sequence, to_sequence   # 仅 30 台账；适配器不得传给引擎
  source_ids, created_at

BackendIngestResult
  subject_id
  stored_marks: Mapping[str, Sequence[str]]   # segment_id → 本轮封存事件 id 列表
  sealed_event_ids: tuple[str, ...]
  role_ids: tuple[str, ...]                   # 仅回传信息；JShi 不得据此建档
  unclosed_count: int
  errors: tuple[str, ...]                     # 非致命
```

约定摘要：

1. **16 的 `text_raw` 永不改写**。30 编成 `MemoryExperience.text` 后再入库；引擎侧 `content_raw` 应等于这份 `text`，端口上不再改写、不切分。  
2. **`objects` 与是否说话无关**；可为空。  
3. **`stored_marks`**：封存几个填几个；本轮完全未封存则不写该项，不算错误。  
4. **`interlocutor`**：用于按说话人软纠偏（防串线）；与 `objects`（泛提及）不同。

### 3.3 读取：`RecalledFragment`

落点：`src/jshi/memory/port.py`。

```text
RecalledFragment
  event_id: str                 # 稳定记忆单元 id（去重键）
  event_type: str               # 进程内附加；REMS 缺则适配器填 "memory"
  text: str                     # 原文，溯源
  content: str = ""             # 按 summary_level 选出的摘要，供组装预算
  summary_level: str | None     # 如 L1；进程内可无
  kind: str = "fact"
  object_id: str | None
  interlocutor: str | None      # REMS 特性；进程内 None
  source_ids: tuple[str, ...]
  score: float = 0.0
  occurred_at: datetime | None
```

`recall` 语义：

- 查询保持原文；`object_id` 可空 = 跨对象（含主体记忆）。  
- **只读 + 记忆恢复**：不新增记忆单元；被召回条目强化遗忘因子是唯一允许的写。  
- 档位 1–9 在进程内映射为条数上限：1–3→3；4–6→5；7–9→8（`recall_level_limit`）。REMS 应用自己的摘要档位逻辑，但返回形状须对齐。

### 3.4 适配器必做的表示层差异

落点：`src/jshi/memory/rems3.py`。

| 点 | JShi | REMS | 适配器 |
|----|------|------|--------|
| Batch | 另有台账字段 | 仅 `subject_id` + `experiences` | 只传后两项 |
| Experience.sub_segments | 无 | 有 | 不填 |
| IngestResult 序列 | tuple | list | 收回转 tuple |
| RecalledFragment.event_type | 常有 | 可无 | 填 `memory` |
| remember_fact | 壳/测试仍用 | 可无单条 API | 合成单条 ingest |
| consolidate / dream | 主链路不调 | 引擎可有 | **不接线** |
| occurred_at | aware UTC 等 | 墙钟 naive | 转本地 naive |
| LLM 密钥 | `JSHI_MODEL_*` | `REMS_LLM__*` | 空则回填 JSHI |

额外透传（非主链路必调）：`portrait`、`assemble_recall_block`。  
`MemoryShell`（`shell.py`）当前**未暴露** `portrait`。

---

## 4. JShi 侧投递与召回时序

### 4.1 投递（写）

```text
活动 → 16 经历段（text_raw + objects + actor/mentioned + source_ids）
  → 07 close（handoff_to_memory_control=True）
  → 30 run_async（后台线程，不挡下一轮）
       evaluate（冲刷策略 220）
       → build_batch（现码在 InProcessMemoryControl，不在 16）
       → ingest_batch
       → 凭 stored_marks 推进 MEMORY 游标 + 台账
  → 历史事件 memory_control_attempted（status / ingest_id / error）
```

冲刷 reason（触发）：

`flush_max_chars` | `flush_max_segments` | `flush_max_idle` | `flush_on_idle` | `flush_night_window`

不触发：

`insufficient_memory_data` | `previous_memory_process_not_finished` | `max_retry_reached`

默认阈值（`InProcessMemoryControl`）：

| 参数 | 默认 |
|------|------|
| flush_max_chars | `active_zone_chars()`（模型窗口 × 1/30；窗口默认 1_000_000） |
| flush_max_segments | 20 |
| flush_max_idle_seconds | 600 |
| flush_on_idle_seconds | 1800 |
| flush_night_window | `"00:00-06:00"` |
| max_retry | 3 |

**同一时间只有一个 ingest 批次在跑**（上一批未结束不触发下一批）。因此邻仓**不必**处理 ingest∥ingest，但**必须**处理 ingest∥recall（见 §8）。

### 4.2 对话行编法（30 → text）

落点：`src/jshi/memorycontrol/inprocess.py` 的 `_experience_text` / `_interlocutor_for`。

- 有 `text_raw`：`说话人名字` + 原文（冠名）。  
- 无言语但有动作：`说话人名字` + `（动作：…）`。  
- 无言语无动作：`text == ""`（允许；不要用 JSON 冒充对话）。  
- `interlocutor`：优先 `actor_object_id`；否则映射表去重后的唯一对方；多人则用 `mentioned_object_ids` 消歧，仍歧义则 `None`。  
- `origin`：内部段 → `internal`；其余 → `external`。

### 4.3 召回（读）

```text
03 MemorySource.load
  → recall(
       query=本轮 input_text,
       object_id=当前说话人,
       level=策略默认档 / 上下文档,
       limit=可选
     )
  → 片段进入组装；可写召回流水 RecallTrace
```

- 05 **不同轮**补召回（同轮补召回已取消）；05 **不**直接调 `ingest`。  
- 策略快照：`{data_dir}/recall_strategy.json`（`RecallStrategy`：default_level / limit / source_report_id）。  
- 召回流水：典型 `{data_dir}/recall_traces.jsonl`（匠石侧，供有效性分析）。

---

## 5. 工厂、环境变量与本地联调

### 5.1 工厂

`src/jshi/memory/factory.py`：

| `JSHI_MEMORY_BACKEND` | 行为 |
|----------------------|------|
| 缺省 / `inprocess` | `InProcessMemoryBackend`（事实历史，无 REMS） |
| `rems3` | `Rems3MemoryBackend`；**缺包立即失败，不静默回退** |
| 其它 | `UnknownMemoryBackendError` |

数据目录：`JSHI_REMS_DATA_DIR` 或 `{JSHI_DATA_DIR}/rems` → `rems.db` + 向量路径；与 `subject.sqlite3` **并列、分离**。

安装邻仓：`pip install -e <Jshi_memory 路径>`。本仓不把 qdrant / sentence-transformers 列入默认依赖。

### 5.2 相关环境变量

| 变量 | 含义 / 默认 |
|------|-------------|
| `JSHI_MEMORY_BACKEND` | `inprocess`（生产 REMS 用 **`rems3`**；勿写 `rems`） |
| `JSHI_REMS_DATA_DIR` | 缺省 `{JSHI_DATA_DIR}/rems` |
| `JSHI_REMS_SKIP_EMBEDDING` | `1/true/yes` → 关语义路，保留词法/对象召回 |
| `JSHI_MODEL_API_KEY` / `ENDPOINT` / `NAME` | rems3 启动时可回填 `REMS_LLM__*` |
| `REMS_LLM__*` | 邻仓 LLM 配置 |
| `REMS_CONTEXT_WINDOW` | 建议与匠石窗口对齐时可设 `1000000`；邻仓 RemsConfig 代码缺省曾为 `88000` |
| `REMS_CHARS_PER_TOKEN` | 常用 `1.5` |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | 离线嵌入 |
| `JSHI_DATA_DIR` | 默认 `.jshi` |
| `JSHI_TEST_REMS` | `=1` 才跑 `@pytest.mark.rems` live |

### 5.3 最小联调步骤（本仓视角）

1. 可编辑安装邻仓；`.env` 设 `JSHI_MEMORY_BACKEND=rems3` 与模型密钥。  
2. `create` 主体 → 多轮 `experience` / `talk`（带 `--speaker` / object）。  
3. 等 30 冲刷（或调阈值 / 等闲时）→ 查 `{data_dir}/rems/` 与历史 `memory_control_attempted`。  
4. 新会话或新进程再问旧事 → 看 03 是否装到片段；可查 `recall_traces.jsonl`。  
5. Live：`tests/live/memory_loop.py` + `tests/live/scenarios/mars-*.json`；适配器 live 测默认 skip，需 `JSHI_TEST_REMS=1`。

---

## 6. 所有权：谁做什么 / 明确不做

### 6.1 Jshi_memory 做

- `ingest_batch`：代谢、边界、封存、对象时间线、索引、`stored_marks`。  
- `recall`：多路召回、融合、遗忘恢复、摘要档位；返回契约形状。  
- 内部未完成 / 残影（**不跨端口**）。  
- 内部 consolidate / dream（可存在，**JShi 未接线**）。  
- `portrait`（REMS 特性）。

### 6.2 Jshi_memory 不做

- 新建 `object_id` / 角色抽取（即便管道仍含 RoleExtraction，**JShi 不调用**）。  
- 改 16 活跃区、个人世界、`subject.sqlite3`。  
- 抽象事件、ASF、`source_events`、频繁项挖掘。  
- 冲刷策略、游标、投递台账（属 30）。  
- 把 `role_ids` 当成「请建档」指令。

### 6.3 JShi 做

- 01 对象建档；16 原文与段；30 冲刷 + `MemoryBatch` + 游标；09 薄壳/工厂/适配器；03 装载；有效性策略；召回流水；评价事件。

### 6.4 硬约束（两边不得破）

1. 原文不改写、不在端口上切分。  
2. 对象只来自 01 映射表。  
3. 无抽象事件；残影不跨端口。  
4. 失败隔离：`errors` 非致命；不回滚 16。  
5. 库分离；换后端不迁数据，游标按新店重来。

---

## 7. 测试矩阵（本仓）

| 路径 | 作用 |
|------|------|
| `tests/test_memory_port.py` | 档位、进程内 recall |
| `tests/test_memory_shell.py` | 壳 + 投递 |
| `tests/test_memory_control.py` | 冲刷、游标、重试、`run_async`/`drain` |
| `tests/test_rems3_adapter.py` | 互转/工厂；live ingest+recall 默认 skip |
| `tests/test_recall_object_bind.py` | objects / interlocutor / 对话行 |
| `tests/test_live_memory_loop.py` | live 脚本逻辑 |
| `tests/live/memory_loop.py` | 循环实测 |
| `tests/live/scenarios/mars-*.json` | ingest / event-recall / quality 场景 |

邻仓契约验收（历史记录）：`tests/unit/test_ingest_batch.py`（主体记忆、objects、原文、`stored_marks`、失败隔离）。

单测解释器注意：本仓 Windows 上应用 Anaconda base 跑 pytest（见 `tests/README.md`），避免 PATH 上错误 Python 假报权限错误。

---

## 8. 并发（邻仓必读）

完整要求见 [`doc/design/rems3-并发要求.md`](design/rems3-并发要求.md)。摘要：

- 同一 `REMSPipeline`：**后台** `ingest_batch` ∥ **主线程** `recall`。  
- 目标：recall 不被 ingest 阻塞；不互毁状态；允许最终一致（可暂读不到未封存内容）。  
- 关注点：共享 SentenceTransformer、SQLite WAL/`busy_timeout`、本地 Qdrant、服务内可变缓存。  
- 只需保证 **ingest∥recall**；ingest∥ingest 由 JShi 串行门禁保证。

---

## 9. 已知债务与文档陷阱

1. **`append_internal` 已实现但主链路/自省未调用**——一旦调用，`origin=internal` 会被 30 投进记忆；设计上自省「现不入 09」，故故意不调。  
2. **REMS live 默认 skip**（`JSHI_TEST_REMS=1` 才开）。  
3. **部分旧文档仍写** `16.build_memory_batch` / `run_once`——**现码**是 `InProcessMemoryControl.build_batch` + 生产路径 `run_async`。  
4. CLI 文偶写后端名 `rems`——工厂值是 **`rems3`**。  
5. 邻仓仍欠（契约 §10.5）：RoleExtraction 仍在管道；`unclosed_count` 多主体隔离；FastAPI/做梦/白描非接入面。  
6. 旧封存无 objects/冠名的数据不回填；新投递才对齐召回过滤。  
7. 进程内事实历史 ≠ REMS 事件；换后端当新店。  
8. `RecallCoordinator` 仍在库内，勿当主路径（同轮补召回已取消）。  
9. 冲刷参数尚未接 14 调参；夜间窗口曾导致测试 flaky（conftest 已钉时钟）。

---

## 10. 情感 / 有效性 / 自省 与记忆的关系（现状）

| 主题 | 现状 |
|------|------|
| 事件级 emotion 字段 | 设计在 09/引擎内部可有；**JShi 当前不产 emotion 投递** |
| 情感模块 | **无**；自省「强烈情绪」触发未接 |
| 遗忘因子 / 情绪共振 | 属引擎内部能力；进程内后端无 |
| 有效性分析 | 读打分 + 召回流水 → 改 `RecallStrategy`；不写记忆库 |
| 自省 300 | 可 `recall`；产出候选；**不**写入记忆 |
| 「再进入 / 反刍」标记 | **尚未实现**（见 §11） |

---

## 11. 近期协作方向（尚未实现，供邻仓规划）

本仓当前工作重心是：**跨会话连续性验证** + 理清「自省起点」与记忆的关系。以下为产品侧设想，**不是现行契约**，落地前需双边再定接口：

### 11.1 问题

自省若继续「罗列触发源」，难以自然；更干净的对偶是：

- 主流程起点 = **外部世界输入**；  
- 自省起点 = **内部时间里，记忆再次浮现（反刍）**。

### 11.2 设想

在记忆单元上增加一层**再进入属性**（名称待定），例如：感情强烈、意味深远、悬而未决、恐怖/忌讳……  

- 主召回：服务当前对话（现有 `recall`）。  
- **并行反刍召回**：按再进入属性，不时把「该再想」的条目推到前台。  
- 自省（300）接住被推上的条目，而不是在 07/工具/边界处各写探测器。

情感系统更适合作为**写标记 / 调强度**的作者之一，而不是自省的唯一起点；「悬而未决」也可由承诺等状态写标记。

### 11.3 对邻仓的含义（若采纳）

- 标记的存储、衰减、与遗忘/AE 的关系 → **引擎侧**。  
- 何时反刍、配额、如何交给 300 → **本仓编排**；可能需要新端口或 `recall` 的 mode/filter 扩展。  
- 在扩展前，请继续保证现有 ingest/recall 契约稳定，并优先满足跨会话召回质量与 §8 并发。

### 11.4 本仓近期会先做的（不依赖新标记）

- 用 `rems3` 跑跨会话召回剧本（投递是否进、召回是否中、对象是否串线）。  
- 分清缺口在「投递/组装」还是「引擎质量」后再提接口变更。

---

## 12. 验收清单（给邻仓自检）

- [ ] `objects` 为空可封存（主体记忆）；非空则进入 `object_ids`。  
- [ ] `content_raw`（或等价）== 输入 `text`；端口不改写。  
- [ ] `stored_marks`：`segment_id → 事件 id 列表`；未封存不写键。  
- [ ] `interlocutor` 可存可回（召回片段能带上，便于软纠偏）。  
- [ ] 无抽象事件 / 不在接入路径调用角色抽取建档。  
- [ ] ingest 失败不污染主体库；`errors` 非致命。  
- [ ] **ingest 进行中 recall 可返回**（见并发文档）。  
- [ ] 不写 `subject.sqlite3`。

---

## 13. 代码落点速查（本仓）

```text
src/jshi/memory/
  backend.py      MemoryBackendPort / InProcessMemoryBackend
  contracts.py    MemoryExperience / MemoryBatch / BackendIngestResult
  port.py         RecalledFragment / recall_level_limit
  factory.py      inprocess | rems3
  rems3.py        Rems3MemoryBackend（互转）
  shell.py        薄壳
  strategy.py     RecallStrategy 快照
  traces.py       RecallTrace
src/jshi/memorycontrol/
  inprocess.py    冲刷、build_batch、run_async、游标
src/jshi/subject/process.py
                  07 后 handoff；03 侧组装调用链
```

---

## 14. 联系方式（协作约定）

- 改端口字段：先改 [`记忆层契约.md`](design/记忆层契约.md)，再改两边类型与适配器；禁止只改一边。  
- 引擎内部算法迭代：可不改契约，但不得破坏 §6 / §12。  
- 需要本仓配合的联调：优先开 `JSHI_TEST_REMS=1` 的适配器 live，或 `tests/live/memory_loop.py` 场景。

— 完 —
