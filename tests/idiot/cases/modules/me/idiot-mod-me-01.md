# idiot-mod-me-01

- 模块：ME 记忆装载
- 人格：`myshkin-init-v1`
- 记忆集：`packs/T1-train-before-probe-a.txt`（译本原文齐后）
- 报告要求：装载清单全文；`history --kind fact` 全文；条数统计

## 步骤

1. create + init
2. 按 pack 逐条写入书中对白（跳过 `#` 注释）
3. 列出事实历史
4. 不跑 experience，或跑后确认 fact 中无“程序生成对白”增量污染 pack 语义

## 期望

- 每条事实文本能在 pack 中找到原文
- 顺序与 pack 一致（或可说明的稳定顺序）
- 条数 = pack 对白行数
