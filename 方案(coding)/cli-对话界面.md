# CLI 对话界面——实现方案

> 对应说明：[doc/CLI使用说明.md](../doc/CLI使用说明.md)（用法，不是模块契约）
> 主链路契约仍以 `doc/design/00` 与 `SubjectProcess.experience` 为准。
> 状态：已确认；最小实现已落地。默认原来的一行输入，`--tui` 为可选壳。
> 分支：`cli/talk-shell`

## 0. 目标与范围

把 `talk` / `chat` 从「一行 `input()` + 元数据混在口头回复后」改成薄终端界面。壳只负责显示与输入；每一句仍调用现有 `process.experience(...)`。

推荐库：**Textual**。不套 Codex / Gemini CLI。不引入 Rust / Node。

### 做

1. 抽出无界面的对话会话对象（斜杠命令、会话文件、调用 `experience` / `preview_state`、把口头回复与调试信息分开）。
2. Textual 全屏壳：上方滚动对话，下方输入，底栏只放元数据。
3. 默认保留原来的一行输入；`--tui` 才套 Textual 壳。缺库或非 TTY 退回原循环。现有 `input()` 测试继续成立。
4. 更新 `doc/CLI使用说明.md` 与 `talk.cmd` 注释：推荐 Windows Terminal，不推荐系统自带命令提示符跑全屏界面。

### 不做

- 不改 `SubjectProcess`、`experience`、`preview_state`、01–16 / 100 的端口与存储。
- 不改 `create`、`experience` 单句、`import-values` 等其它子命令的行为。
- 不改 `.jshi/cli_session.json` 字段含义（仍是 `subject_id` / `speaker`）。
- 不改斜杠命令语义（`/speaker` `/who` `/context` `/plan` `/prompt` `/help` `/quit`）。
- 不把价值库写进对话（仍走现有 CLI 子命令）。
- 不做流式分段（I-001）、不做取消进行中的活动、不做并发生成、不做网页或图形窗口。
- 不在界面里调模型以外的第二条心智路径；`/context` `/plan` `/prompt` 仍只读、不建活动。

### 本次层级

最小：可对话、口头与元数据分离、等待时界面不假死、失败可回退到简易循环。不做侧栏常驻、补全、主题、markdown 渲染增强。

---

## 1. 接口落点

全部落在 `src/jshi/app/`。其它包只被调用，不被修改。

| 文件 | 职责 |
|------|------|
| `src/jshi/app/talk_session.py`（新） | 会话状态与命令分发。不依赖 Textual。 |
| `src/jshi/app/talk_plain.py`（新） | 现有 `input()` 循环，改为调用会话对象。 |
| `src/jshi/app/talk_tui.py`（新） | Textual App。延迟 import。 |
| `src/jshi/app/cli.py` | `talk` 增加 `--tui`（可选壳）与 `--plain`（默认路径的显式写法）。`_runtime`、其它子命令不动。 |
| `pyproject.toml` | 可选依赖 `talk = ["textual"]`，不写入默认 `dependencies`。 |
| `doc/CLI使用说明.md` | 界面、`--plain`、终端要求、安装 `pip install textual`。 |

`cli.py` **模块顶层不 import textual**，避免未安装时拖垮 `create` / `experience` / pytest。

`talk.cmd` 仍执行 `python -m jshi.app.cli talk %*`，不改启动契约。

---

## 2. 数据结构

会话对象（名称以实现时为准，逻辑如下）：

```text
TalkSession
  subject_id, speaker, channel, carriers, data_dir
  last_line, last_plan
  busy: 是否已有一轮 experience 未返回
```

对外只产两类结果，供任何界面渲染：

| 种类 | 用途 | 示例 |
|------|------|------|
| `speech` | 口头回复 | 匠石要说的话；无口头则标明「本轮未开口」 |
| `notice` | 系统/调试 | 错误、`/who`、`/plan` 全文、对象已切换 |
| `meta` | 底栏 | `mode`、对象 label/status、活跃区 version 与段数 |

不把 `meta` 拼进 `speech`。这是当前界面难看的根因，也是本轮唯一的显示契约。

`.jshi/cli_session.json` 仍只写 `subject_id`、`speaker`。不在会话文件里存活跃区或计划。

---

## 3. 行为变更

### 3.1 启动选择（稳定优先）

```text
talk                  → 原来的一行输入
talk --plain          → 同上（显式）
talk --tui            → Textual 全屏壳（需已安装；非 TTY 或缺库则退回一行输入）
```

默认不进全屏。壳只改显示，不改斜杠命令与 `experience`。

### 3.2 对话主路径（与现在相同）

1. 校验主体存在、说话人非空；写入会话文件。
2. 非 `/` 开头的一行 → `process.experience(subject_id, text, object_ref=speaker, channel=..., carriers=...)`。
3. 捕获 `ValueError` 与其它异常，显示为 `notice`，**不中断会话**，不把异常正文当成匠石说话。
4. 成功后更新 `last_line` / `last_plan`，口头走 `action_text`，元数据走底栏。

一轮未返回前：忽略再次提交（输入可留着，但不进第二轮 `experience`）。同一主体串行是主链路现状；界面不得制造并发。

`experience` 在 Textual **worker 线程**里调用，UI 线程只收结果。仓库里 sqlite 均是每次 `_connect()` 新连接，不把长连接绑在 UI 线程。仍禁止两轮重叠。

### 3.3 斜杠命令

语义与现在一致。全屏里输入 `/` 或 `/help` 出现命令列表：上下箭头选择，回车执行，Esc 关闭。`/speaker` 选中后补全到输入框，再写名字。不另开叠层。

### 3.4 其它 CLI

`experience` 单句、价值库、对象、历史等子命令的打印格式与退出码不变。

---

## 4. 测试计划

原则：测试会话逻辑，不测 Textual 像素；不在 CI 默认依赖里安装 Textual。

### 保留（行为不得变）

- `tests/test_external_activity_flow.py`：`test_talk_loop_two_turns_*`、`test_talk_remembers_speaker_*`、`test_talk_inspect_commands_*`（仍 patch `input`，走简易循环）。
- `test_phase0_cli_experience_triggers_full_flow`（单句 `experience`，与 talk 无关）。

### 新增 `tests/test_talk_session.py`

- 普通句调用 `experience` 一次，返回 `speech` 与 `meta`，二者分离。
- `/speaker` 写会话文件，不调 `experience`。
- `/plan` `/context` 不建活动（可用假 `process` 计数）。
- `busy` 为真时第二次提交被拒绝。
- 未知 `/foo` → `notice`，不调 `experience`。

### 不测

- Textual 控件布局、颜色、快捷键。
- 真实模型 HTTP。继续用 Echo（现有环境变量清空方式）。

### 回归

改完后跑 `tests/test_external_activity_flow.py`、`tests/test_talk_session.py`，再跑全套。失败则回退本方案文件所列范围，不改主链路「凑绿」。

---

## 5. 风险与未决

| 项 | 判断 | 处理 |
|----|------|------|
| Textual 与系统命令提示符 | 换行/全屏常差 | 文档写明用 Windows Terminal 或 Cursor 终端；提供 `--plain` |
| 新增依赖 | 默认 `dependencies` 仍为空，pytest 不被迫安装 | 可选 extra `talk`；缺库自动回退 |
| worker 与 sqlite | 每操作新连接，可接受 | 禁止并发 `experience` |
| 进行中无法取消 | 取消等于中断主链路，本轮不做 | 只禁用提交 |
| 界面做太满 | 易把壳做成第二套程序 | 本轮无侧栏、无流式、无工具调用 |
| I-001 流式分段 | 与「等完整一轮再显示口头」冲突 | 不纳入 |
| 记事本「下一份」 | 主链路下一份仍是 REMS3 适配器 | 本分支是 CLI 旁路，不合入主链路计划 |

拍板项（默认按上表，你可改）：

1. 默认原来的一行输入；`--tui` 才套壳。
2. Textual 只作可选依赖，不写进默认安装。

---

## 6. 记录、评价与参数影响

- 记录：不新增主体历史事件类型。界面不写账本。会话文件仍只记对象，与现在相同。
- 评价：本轮无新评价点。对话质量仍由主链路与既有测试覆盖。
- 参数：无新魔法数。不把界面尺寸写入 `params.py`。

---

## 7. 回退

1. 保留 `talk --plain`，行为与提交前的 `input()` 循环一致。
2. 若全屏必须撤销：删除 `talk_tui.py`，`cli.py` 只走 plain；会话对象可留（测试仍受益）。
3. 不需要数据迁移：未改 sqlite 与会话文件格式。
