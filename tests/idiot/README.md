# 《白痴》场景测试树

方案正文见 [`doc/白痴对话测试方案.md`](../../doc/白痴对话测试方案.md)。

## 原则（短）

- **书本权威**：记忆与人格只来自书中材料包。
- **应对零污染**：程序生成的对白只进 `reports/`，不进 `corpus/`、`packs/`、`presets/`。
- **开篇先初始化**：梅什金是成年人；先装 `presets/myshkin-init-v*`，再装记忆。

## 目录

| 路径 | 放什么 | 是否进 git |
|------|--------|------------|
| `speakers.md` | 说话人译名表（预注册对象档案） | 是 |
| `chapter-map.md` | 译本与章回说明 | 是 |
| `corpus/` | 按章/场整理的书中对白原料 | 是 |
| `packs/` | 某测试点冻结记忆集 | 是 |
| `probes/` | 当前输入（对方对白） | 是 |
| `references/` | 书中梅什金对白（软对照） | 是 |
| `presets/` | 开篇 init + 书本变迁 transitions | 是 |
| `cases/modules/` | 按模块的用例 | 是 |
| `cases/scenes/` | 按场景 T0–T5 的用例 | 是 |
| `cases/catalog.md` | 全用例索引 | 是 |
| `runners/` | 装载与出报告脚本 | 是 |
| `reports/` | 详细运行输出 | 否（本地）；优选归档可放 `reports/archive/` |

## 命名

| 种类 | 模式 | 例 |
|------|------|-----|
| 语料 | `ch{章}-{场次}.txt` | `ch01-train.txt` |
| 记忆集 | `{场景}-{简述}.txt` | `T1-train-before-probe-a.txt` |
| 探针 | `{场景}-{序号}.txt` | `T1-a.txt` |
| 对照 | `{场景}-{序号}-myshkin.txt` | `T1-a-myshkin.txt` |
| 用例 | `idiot-mod-{模块}-{序号}.md` 或 `idiot-T{n}-{模块}-{序号}.md` | `idiot-mod-me-01.md` |
| 报告目录 | `reports/<YYYYMMDD>-<git短哈希>/` | `reports/20260731-331547b/` |

对话行格式：

```text
[说话人] 台词原文
```

## 添加新用例的步骤

1. 若缺语料，先补 `corpus/`，再从中切 `packs/` / `probes/` / `references/`。
2. 在 `cases/modules/` 或 `cases/scenes/` 写用例 md，只引用相对路径。
3. 登记到 `cases/catalog.md`。
4. 用独立 data-dir 跑一遍，检查报告里的污染项。
5. 不要把报告里的程序对白拷回语料或人格包。
