# idiot-mod-ex-01

- 模块：EX 外部活动（应对输出）
- 人格：`myshkin-init-v1`
- 记忆集：`packs/T1-train-before-probe-a.txt`
- 探针：`probes/T1-a.txt`
- 书中对照：`references/T1-a-myshkin.txt`
- 报告要求：见方案第 8 节全项；必须含污染检查与对照栏

## 步骤

1. create + init + 装载 pack
2. 记录 personal 条数、fact 历史条数（基线）
3. `preview-state` → 写入报告
4. `experience` → 写入报告（应对全文、ids）
5. 再列 personal / fact 历史；**不得**把生成应对写入 packs/presets
6. 将书中对照全文附于报告并记异同（软）

## 期望

- 有完整应对输出与认知/活动 id
- 污染检查：人格条目不因生成而增加书外内容；记忆集文件未被修改
- 若 fact 历史因 experience 管线增加了当次语言行动，报告须标明“运行时库内痕迹”，且**不得**同步进 `packs/` 作为后序用例养料

## 失败

- 生成对白被写进 corpus/packs/presets
- 后序用例直接复用本用例 data-dir 而未重置
