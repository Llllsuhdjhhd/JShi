# CLI 操作说明

> 依据 `src/jshi/app/cli.py` 的实际参数写，不是凭记忆。命令名、参数、斜杠命令都以本文为准；
> 想核对单个命令：`jshi <命令> --help`。

## 0. 准备

- **Python ≥ 3.11**。核心只用标准库。
- 两种跑法（下面统一写 `jshi`，等价于 `python -m jshi.app.cli`）：
  - 已安装：`pip install -e .` → `jshi ...`
  - 未安装：`$env:PYTHONPATH="src"`（Windows）/ `export PYTHONPATH=src` → `python -m jshi.app.cli ...`
- **数据目录**：全局参数 `--data-dir`（默认 `.jshi`，可用环境变量 `JSHI_DATA_DIR` 覆盖）。
  **测试请单独指一个目录**，别混进正式数据：

```powershell
$env:PYTHONPATH="src"          # Windows
jshi --data-dir .tmp/demo ...
```

- **`.env`（可选）**：键名见 `.env.example`。配了才有「真实对话」和「真工具」；不配也能跑，只是回复会退化成兜底句。

## 1. 起手：建主体 + 注册对象

```bash
jshi --data-dir .tmp/demo create stone --name 匠石
jshi --data-dir .tmp/demo add-object 火星人 --aliases 火
```

`add-object` 会打印 object_id（形如 `OBJ-xxxx`），后面 `--object` 要用。

## 2. 单句（一轮就退）

```bash
jshi --data-dir .tmp/demo experience stone "明天杭州天气怎么样" \
    --speaker 火星人 --object 火星人:OBJ-xxxx
```

参数：`--speaker` 说话人名字；`--object 名字:object_id` 对象映射（可重复）；`--channel`、`--carrier kind:value` 可选。

> ⚠️ **`experience` 一轮就退出进程，而 200 的策划/执行是后台线程**——进程一退，它们就被中断，
> 所以单句模式常常只留下一条 `status=received` 的交接，看不到工具结果。
> **要测工具，用 `talk`（常驻）并等一拍。**

## 3. 对话（常驻，测工具用这个）

```bash
jshi --data-dir .tmp/demo talk stone --speaker 火星人
```

参数：`subject_id` 可省（用上次会话）；`--speaker`、`--channel`、`--carrier`；
`--tui` 全屏界面（需 `pip install textual`）；`--plain` 强制一行输入（默认就是它）。
别名：`chat`。

### 会话内斜杠命令（`/help` 看全部）

| 类 | 命令 | 说明 |
|---|---|---|
| 对象 | `/speaker 名字`、`/who` | 切换 / 查看当前对象（写入会话，下次启动仍有效） |
| 写法 | `/style 名称` | 查看或切换风格包 |
| 状态 | `/context` | 看片场、木头账本与装载（不调模型） |
| | `/last` | 上一轮实际装上的回忆与片场（全文） |
| | `/prompt`、`/prompt 区块名` | 看即将发给模型的 system 与 user（可只看某块） |
| | `/response`、`/response_raw` | 上一轮模型回复（易读 / 解析前原文） |
| | `/plan` | 上一轮 05 的 response_plan 条目 |
| | `/timing [轮数]` | 上一轮各步耗时 |
| **工具** | `/tool` | 工具一览（05 触发 / 200 交接 / 记挂 210 / 包装 / 交付片场 / 引擎反馈） |
| | `/tool now` | 使用中与刚用完的工具 |
| | `/tool list`、`/tool plan`、`/tool raw`、`/tool <id>` | 只列一览 / 205 策划材料 / 反馈少截断 / 指定某一本 |
| 记忆 | `/memory`、`/memory_raw` | 最近一次落库全文 / 尚未交 09 的账本原文 |
| 写入 | `/rule 内容` | 写进提示词【附加规则】（不进长期个人世界） |
| | `/value`、`/boundary`、`/commitment 内容` | 写进个人世界 |
| 权限 | `/login 密码`、`/logout` | 超级权限 |
| 退出 | `/quit`（`/exit`、`/q`） | 结束 |

## 4. 只在命令行看工具过程（不必进对话）

```bash
jshi --data-dir .tmp/demo tool-log stone --speaker 火星人
jshi --data-dir .tmp/demo tool-log stone --speaker 火星人 --now     # 使用中 / 刚用完
jshi --data-dir .tmp/demo tool-log stone --speaker 火星人 --plan    # 205 策划材料
jshi --data-dir .tmp/demo tool-log stone --speaker 火星人 --turn    # 主流程这一拍给了 200 什么
jshi --data-dir .tmp/demo tool-log stone --id <task_id|intake_id>   # 指定某一本
jshi --data-dir .tmp/demo tool-log stone --speaker 火星人 --list    # 只列一览
```

参数：`subject_id` 可省（用上次会话）；`--speaker` 可省（用上次 `/speaker`）；也可直接 `--object-id`；
`--raw` 让引擎反馈少截断。**全部只读**，不调模型、不跑引擎、不改账本。

## 5. 旁路：把一次工具使用直接打给引擎（不经主体过程）

```bash
jshi --data-dir .tmp/demo tool stone --need "查一下杭州天气" --engine pi
jshi --data-dir .tmp/demo tool stone --need "..." --engine pi \
    --params '{"city":"杭州"}' --hang --object-id OBJ-xxxx
```

- `--engine stub|pi`：**默认 `stub`**（确定性占位引擎，只回显需求）。要真执行就显式 `--engine pi`。
- `--template`：匠石侧模板名，仅演示用。
- `--params`：JSON 字符串。
- `--hang`：同步跑完后写入记挂账本；不带则只在终端看结果。

这是**调试旁路**，不经过 05/205/206/210。

## 6. 其它命令

```bash
jshi --data-dir .tmp/demo state stone                    # 身份 + 活跃个人内容
jshi --data-dir .tmp/demo personal stone [--all]         # 个人世界
jshi --data-dir .tmp/demo history stone --limit 20       # 事实 / 主体历史
jshi --data-dir .tmp/demo recall stone "关键词" [--object-id ...]   # 查记忆端口
jshi --data-dir .tmp/demo preview stone "文本" ...       # 只组装、不落库（参数同 experience）
jshi --data-dir .tmp/demo reflect stone "提示"           # 内部反思（别名 inner）
jshi --data-dir .tmp/demo introspect-idle stone          # 300 自省
jshi --data-dir .tmp/demo introspect-candidates stone --limit 10
jshi --data-dir .tmp/demo grant-super 火星人 --password ...
```

100 价值库：`values`、`propose-value`、`review-value <id> accept|reject`、`lock-value`、`supersede-value`、`import-values`、`export-values`。
另：`close <item_id> ...`、`transition <content_id> ...`。
参数细节用 `--help`。

## 7. 测工具的推荐流程

```text
1) 单独数据目录                  --data-dir .tmp/demo
2) 起会话                        talk stone --speaker 火星人
3) 问一句需要外部能力的话         例如「明天杭州天气怎么样」
4) 等一拍（工具在后台跑），再问一句「结果呢」
5) 会话里 /tool now，或另开终端 tool-log ... --now 看过程
```

**看什么**：

- `/tool` 的【200 交接】——`status` 是 `received / launched / failed / proposed`；
- 【记挂 210】——`status`（`open / notified / cancelled`）、`visible`、`summary`；
- 【引擎反馈】——有没有真的 `result`，还是只有 `progress`；
- 【交付片场】——「（尚未交付：仍走 tool_input）」表示还没进片场。

**两个已知坑**：

1. 单句 `experience` 里工具跑不完（见 §2），测试一律用 `talk`。
2. 工具执行默认接 **Pi**；没装 Pi 时 205 会给诚实的失败句「我这边现在连不上工具引擎」，不会假装跑过。

## 8. 环境变量速查

完整键名见 `.env.example`。最常用的：

```text
JSHI_DATA_DIR              数据目录（等价 --data-dir）
JSHI_MODEL_ENDPOINT        模型端点
JSHI_MODEL_API_KEY         模型 key
JSHI_MODEL_NAME            模型名
JSHI_MODEL_THINKING        disabled / low / enabled / medium / high / max / xhigh
JSHI_TOOL_ENGINE           pi（默认）或 stub
JSHI_PI_EXECUTABLE         Pi 可执行文件（Windows 默认 pi.cmd）
JSHI_MEMORY_BACKEND        inprocess（默认）或 rems
JSHI_IDLE_SECONDS          闲时秒数
```
