# CLI 使用说明

本地实验入口。设计契约仍以 `doc/design/` 为准；本文只说明怎样启动、怎样对话、各条子命令做什么。

在仓库根目录操作。数据默认写在 `.jshi/`（已加入 `.gitignore`，不提交）。Windows 可用 **cmd**（命令提示符）或 PowerShell；`talk.cmd` 是给 cmd 写的，两种都能跑。默认仍是原来的一行 `你：` 输入。可选 `--tui` 套全屏壳（推荐 Windows Terminal 或 Cursor 终端，不要用系统自带命令提示符跑全屏）。

---

## 1. 进入方式

### 1.0 cmd 与 PowerShell

先进入仓库根目录。cmd：

```bat
cd /d C:\Users\40575\Desktop\prog\main
```

PowerShell：

```powershell
cd C:\Users\40575\Desktop\prog\main
```

设 Python 包路径时，两种写法不同，不要混用：

| 终端 | 本窗口内设置 |
|------|----------------|
| cmd | `set PYTHONPATH=src` |
| PowerShell | `$env:PYTHONPATH="src"` |

`talk.cmd` 会自己 `set PYTHONPATH=src`，对话不必先设。其它子命令必须先设，否则会报找不到 `jshi`。需要系统能运行 `python`（已在 PATH 里）。

### 1.1 对话（常用）

cmd：

```bat
talk.cmd stone --speaker dp
```

PowerShell 同样可以：

```powershell
.\talk.cmd stone --speaker dp
```

`talk.cmd` 会切到仓库根、设置 `PYTHONPATH=src`，再执行 `python -m jshi.app.cli talk ...`。

含义：

- `stone`：主体 id。须先 `create`（见 §3）。
- `--speaker dp`：本轮说话人（对象引用）。外部输入必须有对象，不能省略。

启动后仍是原来的对话：

```text
直接打字后回车即发送。命令见 /help
主体 stone；对象 dp（长期）
你：
```

在 `你：` 后打字，回车即走完整外部活动（识别 → 组装 → 认知 → 行动 → 收尾）。

第二次起，主体和对象已记在 `.jshi/cli_session.json`，可只运行：

```bat
talk.cmd
```

可选全屏壳（需先 `pip install textual`，或在仓库根 `pip install ".[talk]"`）：

```bat
talk.cmd stone --speaker dp --tui
```

上方滚动对话，下方输入，底栏只放 `mode`、对象状态、活跃区版本。输入 `/` 或 `/help` 后可用上下箭头选择命令，回车执行，Esc 关闭。斜杠命令与主链路与原来相同。未安装或非交互终端会退回一行输入，并在 stderr 提示。`--plain` 与 `--tui` 同时出现时走原来的一行输入。

不用 `talk.cmd`、直接调模块时，cmd：

```bat
set PYTHONPATH=src
python -m jshi.app.cli talk stone --speaker dp
python -m jshi.app.cli chat stone --speaker dp
python -m jshi.app.cli talk stone --speaker dp --tui
```

PowerShell 把第一行换成 `$env:PYTHONPATH="src"`。`chat` 与 `talk` 相同。

结束对话：输入 `/quit`（也可用 `/exit`、`/q`），或 Ctrl+C。

### 1.2 单条命令（不进入循环）

所有非对话操作都用同一入口，不要走 `talk.cmd`。cmd：

```bat
set PYTHONPATH=src
python -m jshi.app.cli <子命令> ...
```

PowerShell：把 `set PYTHONPATH=src` 换成 `$env:PYTHONPATH="src"`。下文凡写 `python -m jshi.app.cli ...`，都假定本窗口已经设过 `PYTHONPATH`。

全局参数（写在子命令前面）：

```bat
python -m jshi.app.cli --data-dir .jshi create stone
```


| 参数           | 默认                                | 说明              |
| ------------ | --------------------------------- | --------------- |
| `--data-dir` | 环境变量 `JSHI_DATA_DIR`，再缺省则 `.jshi` | 身份、主体库、会话文件所在目录 |


列出子命令：

```powershell
python -m jshi.app.cli --help
python -m jshi.app.cli talk --help
```

### 1.3 模型

未配置远程模型时用离线 Echo，只用来跑通框架，回复不是真实认知。

仓库根目录 `.env`（不提交）。进程里已有的环境变量不被 `.env` 覆盖。pytest 运行时不读 `.env`。指定其它文件：`JSHI_ENV_FILE`。

```text
JSHI_MODEL_ENDPOINT=https://api.deepseek.com/v1/chat/completions
JSHI_MODEL_API_KEY=你的密钥
JSHI_MODEL_NAME=deepseek-chat
```

`ENDPOINT` 必须是完整的 `.../v1/chat/completions`，不能只填站点根地址。三项都有才走远程模型；缺一则 Echo。

### 1.4 记忆后端

默认用进程内记忆（`subject.sqlite3` 里的事实历史）。不必另开记忆进程。

可选：同进程接入邻仓 Jshi_memory（包名 `rems`）。先安装邻仓，再设环境变量。缺包时启动直接失败，不会悄悄退回进程内。

安装必须用 **talk.cmd 同一个 python**（机器上常有 Anaconda 与另一份 Python 并存）。在准备跑 `talk.cmd` 的那个窗口里：

```bat
python -c "import sys; print(sys.executable)"
```

把打印出的路径用来安装（不要只用裸的 `pip`，它可能装进另一套环境）：

```bat
python -m pip install -e C:\Users\40575\Desktop\prog\Jshi_memory
```

`.env` 里 `JSHI_MEMORY_BACKEND=rems3` 已经够了。记忆引擎会沿用同一文件里的 `JSHI_MODEL_API_KEY` / `ENDPOINT` / `NAME`（若未另写 `REMS_LLM__API_KEY`）。然后再 `talk.cmd stone --speaker lux --tui`。成功时 stderr 会有一行 `[jshi] memory backend: Rems3MemoryBackend`。

安装时若提示 Scripts 不在 PATH，可忽略，不影响 `import rems`。第一次启动可能下载向量模型，会稍慢。

若报 `No module named 'rems'`，错误里会写出当前解释器路径；用那条路径再跑一次 `-m pip install`。

| 变量 | 默认 | 说明 |
|------|------|------|
| `JSHI_MEMORY_BACKEND` | `inprocess` | `inprocess` 或 `rems3`；其它值报错 |
| `JSHI_REMS_DATA_DIR` | `{data-dir}/rems` | REMS 自己的 sqlite（及可选向量路径），与 `subject.sqlite3` 分开 |

换后端不搬旧记忆。进程内事实与 REMS 事件不是同一份库；`recall` 问的是当前后端。Qdrant 在邻仓默认 `:memory:` 时不必开 Docker。

---

## 2. 对话里怎么用

提示符 `你：` 下（全屏壳为底栏），**不以** `/` **开头的一行都当作对匠石说的话**，走主链路。空行忽略。全屏壳下一轮未返回前再次提交会被忽略，不并发调用。

斜杠命令不调模型（`/context`、`/prompt` 只预览组装）：


| 命令                   | 作用                                           |
| -------------------- | -------------------------------------------- |
| `/help` 或 `/?`       | 列出对话命令。全屏里也可输入 `/`，用上下箭头选择，回车执行 |
| `/who`               | 当前说话人，以及会话文件路径                               |
| `/speaker 名字`        | 更换对象，写入 `cli_session.json`，下次启动仍有效           |
| `/context`           | 预览活跃区正文与五源装载条数；不调模型、不落新活动                    |
| `/plan`              | 上一轮认知的 `response_plan`（mode、条目）。还没说过话则提示先说一句 |
| `/prompt`            | 认知 skill 的提示词骨架；本轮材料仍以 `/context` 为准         |
| `/quit` `/exit` `/q` | 退出循环                                         |


价值观、承诺、对象档案**不要在对话里改**，退出后用 §4–§6 的子命令。

每轮成功后会打印两行（全屏壳把第二行放到底栏）：

```text
匠石：……
[respond；dp/confirmed；活跃区 v2 段3]
```

- `respond` / `wait` / `think` / `ignore`：本轮回复方式。`wait` 仍结束本轮活动。
- 第二段：说话人显示名与识别状态。
- 活跃区版本与段数：账本上的工作上下文（落在 `subject.sqlite3`，关窗口再开仍在）。

### 2.1 重启后会丢什么


| 内容       | 位置                                   | 关窗口再开                |
| -------- | ------------------------------------ | -------------------- |
| 主体身份     | `.jshi/identities.json`              | 还在                   |
| 对象档案     | 与主体库同目录的对象库                          | 还在                   |
| 承诺等个人条目  | `subject.sqlite3` 的 `personal_items` | 还在                   |
| 价值观/边界   | 同一 sqlite 的 `value_entries`          | 还在                   |
| 说话人会话    | `.jshi/cli_session.json`             | 还在                   |
| 经历账本、活跃区 | `subject.sqlite3` 的 `experience_ledger` | 还在。下次 02 载入上一份活跃区 |
| 未投递经历（未过 30 冲刷） | 同一账本 | 还在账本里，不因关窗口丢失 |


对象档案会留下来。同一名字再次出现时，按档案匹配，不必再 `add-object`。新名字第一次说话会新建**暂定**档案（见 §5），不是已确认。未进 09 的近时对话以 16 为准，不要另开一座记忆库。

---

## 3. 第一次建议顺序

cmd：

```bat
cd /d C:\Users\40575\Desktop\prog\main
set PYTHONPATH=src

python -m jshi.app.cli create stone
python -m jshi.app.cli add-object dp
python -m jshi.app.cli import-values stone doc\examples\values-import.example.json
python -m jshi.app.cli add-personal stone commitment "下次继续询问近况"
talk.cmd stone --speaker dp
```

`create` 只做一次。导入价值、加承诺都可省略。`add-object` 也可省略：新说话人第一次开口会自动建暂定对象（§5）。PowerShell 把 `set PYTHONPATH=src` 换成 `$env:PYTHONPATH="src"`，`talk.cmd` 写成 `.\talk.cmd`。

`create` 参数：

```text
python -m jshi.app.cli create <subject_id> [--name 匠石] [--origin 基础型匠石]
```

主体已存在再 `create` 会失败。

---

## 4. 对话与活动（主链路）

### `talk` / `chat`

见 §1、§2。可选：`--channel`、`--carrier kind:value`（可重复，如 `voiceprint:vp-1`）。

### `experience`

单句外部活动，不进循环。适合脚本或偶尔测一句。

```powershell
python -m jshi.app.cli experience stone "今天有些疲倦" --speaker dp
```


| 参数           | 说明                                       |
| ------------ | ---------------------------------------- |
| `subject_id` | 主体                                       |
| `text`       | 本轮原话                                     |
| `--speaker`  | 说话人名字或对象 id；渠道未绑定对象时必填                   |
| `--channel`  | 渠道标识，可选                                  |
| `--carrier`  | `kind:value`，可重复                         |
| `--object`   | 对象映射表 `名字:object_id`，可重复（如 `宝玉:OBJ-BAO`） |


打印行动文本和活动 id、回复 mode。

### `preview-state`

只组装、不调模型、不落库。看进模型前的输入、对象、活跃区、价值/承诺摘要、装载报告。

```powershell
python -m jshi.app.cli preview-state stone "今天有些疲倦" --speaker dp
```

参数与 `experience` 类似（无 `--object`）。对话里的 `/context` 走的是同一套预览。

### `reflect` / `inner`

内部反思，不走对象解析与外部活跃区语义。来源标为系统。

```powershell
python -m jshi.app.cli reflect stone "回顾刚才的交流"
```

反思是占位，不会自动变成长期记忆或价值。

---

## 5. 对象

外部文字必须有对象引用。对话用 `--speaker` / `/speaker`；单句用 `--speaker`。没有对象则拒绝，不落位、不建活动。

**新名字会创建对象。** `--speaker` 或 `/speaker` 给的名字若对不上已有档案（显示名、别名、对象 id 都不命中），这一轮通过门禁后会落一条**暂定**对象档案：新的 `object_id`，`label` 用这次的名字，`status=provisional`。档案在对象库里，关窗口还在。下一次仍用这个名字，按名字匹配这条档案，不再新建。

摘要里第二段会看到 `provisional`（暂定）或 `confirmed`（已确认）。渠道已经指定说话人时，一般按这个人聊，不必每轮问「你是……吗？」；只有重名或对方否认时才会问清是哪一位。

重名（两条档案都叫这个名字）必须消歧，不得任取；消歧不了则阻断，也不会再新建一条来绕过去。

### `add-object`

可选。预先登记、并标成已确认，便于稳定认人。不跑这条命令，只要说过话，新名字照样会按上面规则建暂定档案。

```bat
python -m jshi.app.cli add-object dp --aliases 点心,DP --channel cli --carrier voiceprint:vp-1
```


| 参数          | 说明                   |
| ----------- | -------------------- |
| `label`     | 显示名（必填）              |
| `--aliases` | 别名，逗号分隔              |
| `--channel` | 渠道                   |
| `--source`  | 导入来源，默认 `cli-import` |
| `--carrier` | `kind:value`，可重复     |


打印新对象 id。之后 `--speaker` 可用显示名或该 id。

---

## 6. 价值观与边界（100）

写入独立价值库，**不要**用 `add-personal value`（会被拒绝）。

装载不按当前输入排序。目录变更（导入/审核/加锁/废止）或超过刷新间隔才换快照；间隔默认一天。

JSON 字段说明见 `doc/design/100-价值观与边界.md` §4.2，样例：`doc/examples/values-import.example.json`。

可导入状态：`candidate`、`accepted`、`locked`。`accepted` / `locked` 可进工作集；`candidate` 不能。已存在的 `id` 会跳过，不覆盖。

### `import-values`

```powershell
python -m jshi.app.cli import-values stone doc/examples/values-import.example.json
```

命令行上的 `subject_id` 决定写入哪一个主体；文件里的 `subject_id` 仅作文档标记。

### `values`

列出库内条目（id、角色、状态、摘要或正文）。

```powershell
python -m jshi.app.cli values stone
```

### `export-values`

```powershell
python -m jshi.app.cli export-values stone
python -m jshi.app.cli export-values stone --out values.json
```

不写 `--out` 则打印到终端。

### `propose-value`

提出**候选**，默认来源 `human_intervention`。须再 `review-value accept` 才能装载。

```powershell
python -m jshi.app.cli propose-value stone "与朋友交，言而有信。" --summary 关于诚信 --source-type classic_work
```


| 参数                           | 说明                                                                                                                      |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--role`                     | `value`（默认）或 `boundary`                                                                                                 |
| `--summary` / `--stance`     | 概括、态度                                                                                                                   |
| `--source-type`              | `classic_work` / `classic_novel` / `model_proposal` / `human_intervention` / `derived_experience` / `governance_review` |
| `--source-id`                | 来源对象，可空                                                                                                                 |
| `--importance`               | 重要程度，默认 1.0                                                                                                             |
| `--binding` / `--no-binding` | 仅边界：是否强制；缺省为强制                                                                                                          |


打印候选 id。后续命令用这个 id。

### `review-value`

```powershell
python -m jshi.app.cli review-value <item_id> accept 讨论定稿 --reviewer 你的名字
python -m jshi.app.cli review-value <item_id> reject 来源不足
```

只有 `candidate` 可审。`--reviewer` 默认 `cli`。

### `lock-value`

已接受（或旧数据里仍为 `active`）的条目加锁。

```powershell
python -m jshi.app.cli lock-value fabeb504-989e-4dd8-8be6-ab56f3bf0dd0 稳定下来 --approver 你的名字
```

### `supersede-value`

用已在库中的另一条废止旧条。

```powershell
python -m jshi.app.cli supersede-value <旧id> <新id> 被新原则替代
```

人工流程：改 JSON → `import-values`（新 id）或 `propose-value` → `review-value` → 需要时 `lock-value` / `supersede-value` → `talk` 里 `/context` 看是否装上。

---

## 7. 其它个人内容（08 占位种类）

`add-personal` 可写承诺、关系、能力、审美、自我理解等，**不能写 value**。

```powershell
python -m jshi.app.cli add-personal stone commitment "下次继续询问近况" --importance 1.0
```

`kind`：`memory` / `value`（拒绝）/ `relationship` / `commitment` / `capability` / `aesthetic` / `self_understanding`。

### `close-personal`

结束承诺等（按条目 id，不按主体）。

```powershell
python -m jshi.app.cli close-personal <item_id> completed 已经履行
```

状态：`completed` / `released` / `superseded`。

### `personal`

列出 `personal_items`（**不含** 100 价值库）。看价值用 `values`。

```powershell
python -m jshi.app.cli personal stone
python -m jshi.app.cli personal stone --kind commitment --all
```

`--all` 含已结束条目。

### `state`

身份摘要 + 上述个人条目条数（同样不含价值库）。

```powershell
python -m jshi.app.cli state stone
```

---

## 8. 历史、回忆、认识状态

### `history`

主体仓里的事实/主体历史（不是对话气泡记录）。

```powershell
python -m jshi.app.cli history stone
python -m jshi.app.cli history stone --kind fact --limit 20
python -m jshi.app.cli history stone --kind subject
```

### `recall`

问当前记忆端口。默认进程内实现；`JSHI_MEMORY_BACKEND=rems3` 时问的是邻仓引擎，不是 `subject.sqlite3` 里的旧事实。

```powershell
python -m jshi.app.cli recall stone "朋友" --level 1 --object-id OBJ-...
```

`--level` 为 1–9；未指定 `--limit` 时由档位决定条数。

### `transition`

改一条**已落库**认知条目的认识状态（感知/反思等）。主链路对外契约是 `response_plan`，一般对话不必用。

```powershell
python -m jshi.app.cli transition <content_id> provisional "目前证据有限"
```

目标状态：`considering` / `provisional` / `accepted` / `rejected` / `suspended` / `revised`。允许的迁移以代码为准，非法迁移会报错。

---

## 9. 常见情况


| 现象                        | 原因与做法                                  |
| ------------------------- | -------------------------------------- |
| 找不到 `jshi` | 未设 `PYTHONPATH`。cmd 用 `set PYTHONPATH=src`，PowerShell 用 `$env:PYTHONPATH="src"`；对话也可用 `talk.cmd` |
| 主体不存在                     | 先 `create`                             |
| 未指定说话人                    | 首次 `talk` 加 `--speaker`；之后用 `/speaker` |
| `add-personal` 写 value 被拒 | 改走 `import-values` / `propose-value`   |
| 导入跳过                      | 同 id 已在库中；换 id 或先导出核对                  |
| 没配密钥却像在「复读」               | 三项模型环境变量不齐，走了 Echo                     |
| `JSHI_MEMORY_BACKEND=rems3` 报没有 rems | 装进了另一套 Python。在同一窗口用 `python -c "import sys; print(sys.executable)"`，再对该解释器 `python -m pip install -e <Jshi_memory>` |
| 换了记忆后端后 recall 变空           | 库不共用、不自动迁移；这是预期                       |
| 重启后忘了刚才在说什么               | 活跃区在内存；身份和价值库仍在                        |
| `/context` 活跃区为空          | 本进程还没成功说过话，或刚重启                        |


模块编号与主链路顺序见 `doc/design/00-流程总览与输入边界.md`。价值观字段见 `doc/design/100-价值观与边界.md`。