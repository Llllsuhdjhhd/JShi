# CLI 耗时旁路与长回复折行——实现方案

> 对应：`方案(coding)/cli-对话界面.md`、想法 I-012 / I-013；耗时对齐 I-005（只观测）
> 状态：待确认（确认前不动代码）
> 本轮目标：能回看上一轮各步耗时；长口头回复按窗口折行。对话默认不刷阶段表。

## 0. 独立判断

慢往往混在一句「回复很久」里：⑤ 可能两轮模型、rems3 冷启动、30 冲刷再调记忆侧模型。没有分步记录就无法区分。观测不应进入经历或模型上下文，也不该每轮印在对话里。

折行是壳的问题：Textual `Log` 默认不 wrap。不改主链路。

## 1. 目标与范围

### 做

1. `experience` 一轮内给这些步骤打时间戳：落位、02 装载、03 组装、建活动、05 认知（分第一次 / 追加召回后第二次）、06、10、16 编排、07、30。无追加召回则第二次记 0 或不出现。
2. 记录留在进程内环形缓冲（最近 N 轮，N=20 足够），**不写**经历段、**不写**主体历史、**不进**模型。
3. `/timing`：上一轮各步毫秒 + 合计。`/timing 5`：最近最多 5 轮，每轮一行摘要 + 可展开的分步（先每轮一块分步即可，不必两级交互）。
4. TUI `Log(..., wrap=True)`（或当前 Textual 等价 API）。plain 循环对 `speech` 按 `shutil.get_terminal_size().columns` 折行；`notice` 同样折。
5. `/help` 与命令列表加上 `/timing`。底栏仍只放对象信息。

### 不做

- 不每轮自动打印耗时。
- 不把耗时写入 sqlite / 经历 / 09。
- 不改输入框折行。
- 不做火焰图、不做跨进程汇总（关 talk 即丢缓冲，可接受；与 I-010 落盘无关）。

## 2. 接口落点

| 位置 | 变更 |
|------|------|
| `src/jshi/subject/process.py` | 一轮结束把 `timing` 字典挂在返回值或 `process.last_activity_timing`；内部用单调时钟 |
| `src/jshi/app/talk_session.py` | 缓冲；`/timing`；`TALK_COMMANDS` |
| `src/jshi/app/talk_tui.py` | `Log` 折行 |
| `src/jshi/app/talk_plain.py` | 按列宽折 `speech` / `notice` |
| `doc/CLI使用说明.md` | `/timing`、折行一句（方案确认后写） |

步骤名用中文短标签，与七阶段对应，避免对用户暴露模块编号作唯一说明（可并行写 ①③⑤）。

## 3. 数据结构

```text
ActivityTiming
  activity_id
  started_at   # 墙钟，仅供回看
  steps: ((name, milliseconds), ...)
  total_ms
```

`TalkSession._timings: list[ActivityTiming]`，超出 20 条丢最旧。

## 4. 测试计划

`tests/test_talk_session.py`：

- 假 process 返回耗时 → `/timing` 含步骤名与合计，不调 `experience`。
- `/timing 2` 出两轮。
- 普通句之后对话事件里**没有**阶段表。

`tests/test_subject_process.py` 或窄测：一轮走完 `last_activity_timing` 含认知与收尾（可用 Echo，30 不冲刷时 30 一项可为 0）。

不测 Textual 像素。plain 折行可测纯函数（给定宽度 20、长串被切成多行）。

## 5. 风险与未决

| 项 | 判断 | 处理 |
|----|------|------|
| 步骤漏记 | 只记主链路已有函数边界 | 不插入 01 内部匹配耗时 |
| 关进程丢失 | 故意 | 不落盘 |
| Textual wrap API 因版本而异 | 查已钉的 textual 版本 | 以 `wrap=True` 或 CSS `text-wrap: wrap` 择一 |

拍板：缓冲 20 轮、默认 `/timing` 只看上一轮。

## 6. 记录、评价与参数

- 记录：进程内旁路。不进 00 总览的主体历史表。
- 评价：无。
- 参数：缓冲长度写死 20，不进魔法书。

## 7. 回退

去掉 `/timing` 与时钟埋点；`Log` 恢复默认。无数据迁移。
