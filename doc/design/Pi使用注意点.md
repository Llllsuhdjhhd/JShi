# Pi 使用注意点（206 引擎侧）

> 只讲**用**，不讲装。装法保持现状、不再研究：Windows 全局 npm `@earendil-works/pi-coding-agent@0.85.1`，入口 `dist/bundle/cli.js`，由 `pi.cmd` 经 node 拉起。
>
> 基准：2026-09-12 复验。全套 `2 failed, 409 passed, 1 skipped`（两条失败与 Pi 无关）。
>
> **证据分级**：**【实测】**＝本轮亲自跑出来的；**【源码】**＝读 `pi_engine.py` / `cli.py` 等得出的事实；**【上轮】**＝上一轮报告的实测，本轮未复现（照录，不当已验证）。

## 0. 一句话

**链路通，但 Pi 在这套沙箱里干不了活。** 这两件事要分开看：RPC 与事件翻译已验证可用；Pi 自己的 shell 工具一执行就撞沙箱。不要把后者误读成「适配器坏了」。

## 1. 最要命的坑：失败是静默的

### 1.1 列目录失败 → 空目录，不报错 【源码】

`PiEngine.list_commands()` 里是 `except OSError: return ()` 加 `except Exception: return ()`。可执行文件写错、Pi 起不来、超时——**全都表现成「目录里没有工具」**。

上一轮那个「看起来像目录里没有工具」就是这么来的（可执行名写成 `pi` 而不是 `pi.cmd`）。

**判断口径**：`list_commands()` 返回空时**不能**直接当作「引擎没有工具」，要同时看 `_last_stop`（`timeout` / `cancelled` / 空）。本轮正常返回时 `_last_stop == ""`。**【实测】**

### 1.2 Windows 上不存在无扩展名的 `pi` 【实测】

npm 只生成 `pi.cmd` / `pi.ps1`。`subprocess` 不走 PATHEXT 解析，所以找不到 `pi`。206 默认已用 `pi.cmd`（非 Windows 用 `pi`），可用 `JSHI_PI_EXECUTABLE` 覆盖。

### 1.3 `home_dir` 是 CWD 相对的 【源码】

`cli.py` 传 `None` → `PiEngine` 落回 `Path(".tmp/pi")`，**相对当前工作目录**。换个目录启动就会落到工作区外，撞沙箱（见 §3）。本轮是在仓库根跑的，所以没撞。

## 2. 会话生命周期：不按这两条会挂

### 2.1 Pi 答完不退出 → 不能等 stdout EOF 【源码】

旧写法「等 stdout 关闭」会永远等下去（上轮探测挂了 300 秒）**【上轮】**。现在靠两个停止条件收工：

- `stop_on_command="get_commands"`：列目录收到目标 `response` 即返回；
- `stop_on_settled=True`：执行收到 `agent_settled` 即返回。

本轮 `list_commands` 1.33 秒返回、`_last_stop` 为空，**确定不是超时兜底**。**【实测】**

### 2.2 杀进程要杀整棵树 【源码】

`pi.cmd` → `cmd.exe` → `node`。只杀直接子进程会留下孙进程握着 stdout。`_kill` 先 `taskkill /PID <pid> /T /F`，再兜底 `kill()` / `terminate()`。

### 2.3 即使杀不掉，也必须在 deadline 内收回 【源码】

读循环是「后台线程读行进队列 + 主循环按墙钟 deadline 取」，所以**杀不干净也一定返回**，卡不死。终态落点：

| 结束原因 | 落点 |
|---|---|
| `timeout` | `result/failed`「工具执行超时」 |
| `cancelled` | `result/aborted`「已取消」 |
| 都不是且无结果 | `result/failed`「Pi 会话未产出工具结果」 |

## 3. 沙箱边界：决定 Pi 现在能干什么

### 3.1 Pi 执行工具会 `spawn EPERM` 【上轮，本轮未复现】

上轮实测：让 Pi 真调一次工具 → `tool_execution_start(bash)` → `tool_execution_end(isError=true, "spawn EPERM")`。原因是 Pi（node）要**抓子进程输出 = 管道**，而本沙箱两种受限模式都禁命名管道。与 harness 记录的边界一致。

**本轮没有亲自复现这一条**，按上轮记录转述。

### 3.2 换 shell、换工具都不解决 【源码 / 文档】

Windows 上 Pi 默认走 Git Bash，也支持 `powershell` 工具（`defaultTools`）。但**两者都要用管道抓输出**，边界一样。所以换 `shellPath`、把 `bash` 换成 `powershell`，都绕不过 EPERM。

### 3.3 结论

- **策划段能验**：`get_commands` 不发子进程，已实测通；
- **执行段不能**：要真干活必须跳沙箱，`danger-full-access` 逐次放行。

所以「只验策划、不碰执行」（205 §8.5 的选项 2）是当前唯一不需要放行的路子。

## 4. Windows shell 前置 【实测】

- Git for Windows **已装**：`C:\Program Files\Git\bin\bash.exe`。Pi 按 `settings.json` 的 `shellPath` → 该路径 → PATH 的顺序找，所以当前走的是 Git Bash。
- **注意**：本机 PATH 上的 `bash` 是 `C:\WINDOWS\system32\bash.exe`，那是 **WSL**。若哪天 Git Bash 不在了，Pi 会静默退到 WSL bash——**另一个文件系统、另一套环境**，行为会变，且不会报错。
- 当前 `~/.pi/agent/settings.json` 内容只有 `{"lastChangelogVersion":"0.85.1"}`，即**没有自定义 `shellPath`**。

## 5. 目录与能力边界

### 5.1 `get_commands` 只给 name + description 【源码】

外加 `sourceInfo.source` / `sourceInfo.path`。**拿不到 SKILL.md 正文，`describe` 也变不出参数**。点名之后的真实发挥在 Pi 会话里，不在检索层。这是引擎能力边界，检索补不了。

### 5.2 当前落点没有 skill / extension → 目录只有 1 条 【实测】

`.tmp/pi/agent` 与 `~/.pi/agent` **两边都没有 `skills/` 或 `extensions/`**。真目录：

```text
llama  Manage llama.cpp router models  source=inline  path=<inline:llama.cpp>
```

挪落点（`PI_CODING_AGENT_DIR`）**没丢任何对目录有影响的东西**：只少了 `settings.json`（就一行 `lastChangelogVersion`）和 `bin/`（fd / ripgrep 下载缓存，Pi 会自己再拉）。

### 5.3 `sourceInfo` 一路走到了 `Disclosure.sources` 【实测】

`inline` 不只是躺在 entry 里：`ToolIndex.disclose().sources == [{"source":"inline","count":1}]`。对照 Stub 是 `[{"source":"engine","count":1}]`。

### 5.4 `total ≤ 8` → `tier=eager` → 检索分支不可达 【实测】

`DIRECT_MAX_TOOLS = 8`。当前 `total=1` ⇒ `tier=eager`，**整表直接交给模型**，`search` / `describe` 那条 BM25 路径**一次都不走**。

真 Pi 目录实测：

| query | 结果 |
|---|---|
| `天气` / `weather` / `查一下明天杭州的天气` / `机票` | 全空，hint「这些词目录里都没有」 |
| `llama` | 1 命中，score `50.3956`（`NAME_EXACT_BOOST` 50 + BM25） |
| `describe(["llama","weather"])` | `tools=[llama]`，`not_found=[weather]` |

**后果**：只跑真 Pi，验到的是 eager 分支；**检索分支目前只有单测的假目录覆盖得了**。

## 6. 事件翻译

- **只有 `tool_execution_start` / `update` / `end` 被译**，其余事件（含思维链类）**丢掉**，不要漏进记挂。**【源码】**
- `update` 的 `partialResult.content` 累计进 `progress.partial`；`end` 的 `isError` 决定 `result/ok` 还是 `result/failed`。**【源码】**
- **不要改成攒到 `agent_settled` 再整包写**（设计口径见《工具使用》§10）：要边读边入记挂。
- 完整事件流本轮未在真 Pi 上跑；上轮记录为 `response / agent_start / turn_start / message_start / message_end / message_update:text_start / text_delta / text_end / turn_end / agent_end / agent_settled`。**【上轮】**

## 7. 凭据

- Pi 内置 DeepSeek provider，provider id 就是 `deepseek`，环境变量名 `DEEPSEEK_API_KEY`；本仓 `.env` 里叫 `JSHI_MODEL_API_KEY`，206 会透传（`env_api_key_name`）。**【源码】**
- `_base_args` 把 key 走 **`--api-key` 命令行参数**，不只是环境变量。Windows 上同机进程可读到命令行（`tasklist` / WMI）。要收紧得改 206，目前没改。**【源码】**
- 子进程另外 `setdefault` 了 `PI_OFFLINE=1` 与 `PI_SKIP_VERSION_CHECK=1`（已有则不覆盖）。**【源码】**

## 8. 两个超时，别混

| 参数 | 默认 | 管什么 | 超时落点 |
|---|---|---|---|
| `list_timeout_s` | 5 秒 | `get_commands` | 空目录（**静默**，见 §1.1） |
| `timeout_s` | 120 秒 | 一次执行的读循环 | `result/failed`「工具执行超时」 |

单测覆盖了「超时落 failed」（本轮随全套测试通过）**【实测】**；上轮另有「75 秒准时落 failed」的实测记录**【上轮】**。

## 9. 记账（**未动代码**）

1. `list_commands` 失败静默 → §1.1，最该修的一条。
2. `home_dir` CWD 相对 → §1.3。
3. `--api-key` 走命令行 → §7。
4. 检索分支在真配置下不可达 → §5.4；若目录长期只有 1 条，205 的分层披露是**为未来的目录写的**，值不值要拍。
5. Pi 执行需跳沙箱 → §3.3。「让 Pi 造工具」（205 §8.5）在沙箱内做不到，必须先解决这一条。
