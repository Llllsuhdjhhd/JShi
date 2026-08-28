# 09 REMS3 薄适配器——实现方案

> 对应文档：[doc/design/记忆层契约.md](../doc/design/记忆层契约.md)（双边约定权威）、[doc/design/09-记忆系统.md](../doc/design/09-记忆系统.md)（09 薄壳）。进程内最小实现已落地，见 [09-记忆系统.md](09-记忆系统.md)，本文不覆盖那份方案。邻仓：`C:\Users\40575\Desktop\prog\Jshi_memory`（包名 `rems`）。状态：**已确认**。最小接线落在分支 `memory/rems3-adapter`。默认仍进程内；`rems3` 缺包则启动失败。

## 0. 全局与依赖检查

- 稳定原则：正常活动不无声改写长期个人世界；记忆写入只经 30；对象来自 01。
- 《想法记录》：I-001–I-007、I-009 不纳入。I-005 旁注：后端选择是启动参数，不在主流程里实时计算。
- 记事本「下一份」原为 REMS3 薄适配器；本文是该条目的方案。30 冲刷参数暴露另开，不塞进本次。
- Jshi_memory 已实现 `rems.port.MemoryBackendPort`（`ingest_batch` / `recall`）。管道里仍有角色抽取、FastAPI、做梦任务，不是本次接入面。

## 1. 目标与范围

本次做到**最小接线**：本仓库加一层同进程适配器，把 30 / 03 / 05 已在用的 dataclass 端口接到 `REMSPipeline`，默认仍走进程内兜底。两边仓库不合并，库不共用。

### 做

1. `Rems3MemoryBackend` 实现本仓 `MemoryBackendPort`：懒加载 `rems`，只做形状互转。
2. 工厂按环境变量选后端；CLI `_runtime` 注入 `SubjectProcess(memory=...)`。
3. 互转单测用假对象，不依赖真实 `rems`。现有记忆测试继续打进程内。
4. CLI 使用说明补一小节：环境变量、同进程、不必另开记忆服务。



### 不做

- 不把 `src/rems` 拷进本仓库，不 git subtree，不启 FastAPI / uvicorn。
- 不把 REMS 设为默认后端；不把 qdrant / sentence-transformers 写入默认 `dependencies`。
- 不改 03 / 05 / 07 / 30 行为；不接线 `consolidate` / `dream`。
- 不清 REMS 角色/抽象债；不把 `subject.sqlite3` 里的进程内事实迁进向量库。
- 不合并两边的 `MemoryExperience` 类型（JShi dataclass，REMS pydantic）。



### 本次层级

最小：可选启用、缺包则启动失败（不静默退回）、测试默认不装 `rems`。不做 HTTP 双进程、不做数据迁移。

## 2. 接口落点

不改 03 / 05 / 07 / 30。`SubjectProcess` 未注入 `memory` 时仍默认 `MemoryShell(InProcessMemoryBackend(...))`。


| 落点                                        | 动作                                                                  |
| ----------------------------------------- | ------------------------------------------------------------------- |
| `src/jshi/memory/rems3.py`（新）             | `Rems3MemoryBackend`：`ingest_batch` / `recall` / `remember_fact` 互转 |
| `src/jshi/memory/factory.py`（新，或并入 rems3） | `build_memory_backend(repository, data_dir)` 读环境变量                  |
| `src/jshi/memory/__init__.py`             | 导出工厂；**不**在模块顶层 `import rems`                                       |
| `src/jshi/app/cli.py` `_runtime`          | `memory=MemoryShell(factory(...))` 注入进程                             |
| `src/jshi/subject/process.py`             | 不改默认兜底                                                              |
| `pyproject.toml`                          | 可选 extra `memory-rems` 仅作标记（可空）；不写死邻仓路径                             |
| `doc/CLI使用说明.md`                          | 环境变量与「只需启动 talk」                                                    |
| `tests/test_rems3_adapter.py`（新）          | 互转单测；可选 `@pytest.mark.rems` 默认 skip                                 |


`process.py` 与主链路其它文件不要 `import rems`。

## 3. 数据结构

无新主体表。REMS 自有库与向量，路径 `{data_dir}/rems/`，与 `subject.sqlite3` / `identities.json` 并列。

形状互转见《记忆层契约》§10。要点：

- 30 的 `MemoryBatch` 多出 `batch_id` / `from_sequence` / `to_sequence` / `source_ids`：只把 `subject_id` + `experiences` 交给 REMS。
- `sub_segments` 不由适配器填写，管道合并时自建。
- 收回的 list 转成本仓 tuple；`RecalledFragment.event_type` 缺则填 `memory`。
- `remember_fact`（测试播种）合成单条 `ingest_batch`。
- `role_ids` 原样回传，01 不据此建档。



## 4. 行为变更

```text
SubjectProcess
  → MemoryShell
  → factory
       缺省 / inprocess → InProcessMemoryBackend（现状）
       rems3            → Rems3MemoryBackend → REMSPipeline（同进程）
```

- `JSHI_MEMORY_BACKEND`：缺省或 `inprocess` → 进程内；`rems3` → 适配器。其它值：启动报错。
- `rems3` 且 `import rems` 失败：**立即退出**，不静默退回进程内。
- 启动 talk / experience 仍只开一个进程；不必另开记忆服务。Qdrant 配置为 `:memory:` 时不必 Docker。
- 换后端不迁数据。进程内事实与 REMS 事件不是同一店；30 游标按新店重来（旧游标指向的段对 REMS 无 `stored_marks` 意义，需人工或复位，不在本次做自动迁移）。

安装（文档，不写进 pyproject 路径）：

```powershell
pip install -e C:\Users\40575\Desktop\prog\Jshi_memory
# 仓库根 .env 增加一行（PowerShell 不能写 KEY=value）
# JSHI_MEMORY_BACKEND=rems3
# 本窗口临时：$env:JSHI_MEMORY_BACKEND="rems3"
```



## 5. 测试计划

默认 pytest（不装 `rems`）必须绿：

- 新增 `tests/test_rems3_adapter.py`：用简易命名空间 / 假对象测 dataclass ↔ 端口形状（批次剥台账字段、结果 list→tuple、`event_type` 缺省、`remember_fact` 合成单条批次）。
- 工厂：缺省 → InProcess；未知值 → 抛错；`rems3` 且无 `rems` 模块 → 抛错（可用 `monkeypatch` 挡 import）。
- 现有 `tests/test_memory_port.py`、`test_memory_shell.py`、`test_memory_control.py`、主流程记忆断言：**不改后端**，继续进程内。

可选（默认 skip）：

- `@pytest.mark.rems`：本机已 `pip install -e` 邻仓且设 `JSHI_TEST_REMS=1` 时，对真实 `REMSPipeline.from_config` 打一条 ingest + recall。CI 不跑。

回归：全套 pytest（`py3125`，basetemp 仍 `.pytest/tmp`）。夜间窗口导致的既有 flaky 不在本次修。

## 6. 风险与未决

- REMS 管道仍装配 `RoleExtractionSkill` / `query_role`：约定禁止 JShi 调用；若内部代谢仍用名字建角色，那是邻仓债务，适配器不绕、不喂抽取。
- `unclosed_count` 是否严格按 `subject_id` 过滤：多主体前须 REMS 侧修；当前 CLI 单主体可先用。
- 代谢可能调 LLM（邻仓默认端点）：适配器只注入数据目录与配置入口，不在本次统一两套 API Key。
- 30 游标与 REMS 未完成缓冲：契约已规定「整批未封存则推进到批次末端」。适配器不改 30。
- 换引擎后旧回忆消失：预期行为，须在 CLI 说明里写明。



## 7. 记录、评价与参数影响

- 记录点：不新增历史 kind。后端名字可写入启动日志（实现时一行即可），不进经历账本。
- 评价点：本次不接 14。召回质量变化属换引擎，不与进程内基线混比。
- 参数：`JSHI_MEMORY_BACKEND`（`inprocess` | `rems3`）；可选 `JSHI_REMS_DATA_DIR`（缺省 `{data_dir}/rems`）。属启动快照（I-005），不在活动中重算。



## 8. 回退

去掉环境变量或设为 `inprocess` 即回进程内。适配器文件可留。不删邻仓、不改已有 `subject.sqlite3` 事实表。