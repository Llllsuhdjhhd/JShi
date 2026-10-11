# 提示词索引

本目录集中维护本项目的提示词说明、全文和实际例子。索引先列出项目中的提示词，具体文档按用户逐次指定的范围完善。

## 编号与索引

编号使用“分组 + 留空序号”，例如 `P20-10`。组内通常以 10 为间隔；两项之间可插入 `P20-15`，空间不够时可用 `P20-15a`。新模块可使用空闲分组。编号用于引用，不强制连续，也不因插入、改名或移动而给已有条目重新编号。具体文档命名为 `P20-10-提示词名称.md`。

按流程阅读：**输入观察 → 入口 JEV → 主认知 → 工具 → 写场 → 后台人物/记忆整理 → 自省**。后台环节可异步运行，并非每轮都执行。初始化提示词放在写场组内，共用片段单独列在末尾。

以下根据 2026-10-10 工作区的 `src/jshi` 实现整理，包含完整指令、分支版本、动态输入模板和共用片段；它们不是同等粒度的独立模型调用。代码链接指向定义或组装位置。已完善条目在名称处链接具体文档，并注明例子数量和最近更新；未标注的条目均为 **待完善**。

### P10 · 输入观察与入口判断（预留 P10-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P10-10 | 视觉画面描述 | 照片或视频采样先转为环境材料，可附观察问题 | [vision/model.py](../../src/jshi/vision/model.py) · `VisionModel.describe` |
| P10-20 | 视觉环境变化判断 | 环境观察分支追加结构化描述、变化程度与依据 | [vision/model.py](../../src/jshi/vision/model.py) · `observe` / `describe(structured=True)` |
| P10-30 | [第一次 JEV：人物终审、档位、历史回忆与短聊路由](P10-30-第一次JEV.md) | 输入进入主认知前；短聊条件满足时可直接回答 | [voice/jev_prompts.py](../../src/jshi/voice/jev_prompts.py) · `ENTRY_INSTRUCTION`；已完善，1 个历史完整例子，当前版匹配例子待补；2026-10-10 |
| P10-40 | [第一次 JEV 的本批输入模板与输出约束](P10-30-第一次JEV.md#最终请求如何组装) | 将信封、发言、声音证据、片场、播放状态和短聊材料送入入口判断 | [voice/jev.py](../../src/jshi/voice/jev.py) · `decide_batch`；已并入 P10-30 完善，共用其 1 个历史例子；2026-10-10 |
| P10-70 | 单条 JEV：插话与播放接续判断 | 单条判断入口，选择忽略、续播、停止、回应或澄清 | [voice/jev.py](../../src/jshi/voice/jev.py) · `decide`；与批量入口分开保留 |

### P20 · 主认知与人格回应（预留 P20-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P20-10 | 木头：默认主认知 | 默认人格的本轮认知与回应 | [skill/cognition.py](../../src/jshi/skill/cognition.py) · `CognitionSkill.instruction` |
| P20-20 | 斯密斯：L1 完整回应 | 主认知完整档，保留工具能力 | [style/packs.py](../../src/jshi/style/packs.py) · `SMITH_REPLY_INSTRUCTION` |
| P20-30 | 斯密斯：L2 无工具回应 | 去工具档，保留较完整的理解与交往规则 | [style/prompt_levels.py](../../src/jshi/style/prompt_levels.py) · `MEDIUM` |
| P20-40 | 斯密斯：L3 简短回应 | 无工具的简写交往档 | [style/prompt_levels.py](../../src/jshi/style/prompt_levels.py) · `SHORT` |
| P20-50 | 苏西坡：回应 | 小说家人格的本轮回应 | [style/packs.py](../../src/jshi/style/packs.py) · `SUXIPO_REPLY_INSTRUCTION` |
| P20-60 | 阿丘：回应 | 阿丘人格的本轮回应 | [style/packs.py](../../src/jshi/style/packs.py) · `AQIU_REPLY_INSTRUCTION`；初始化与写场复用斯密斯 |
| P20-80 | 斯密斯：完整人格底本 | 人格基础指令，回应专用指令为空时的回退来源 | [style/packs.py](../../src/jshi/style/packs.py) · `SMITH_INSTRUCTION` / `SMITH_BLOCK` |
| P20-90 | 苏西坡：完整人格底本 | 人格基础指令，回应专用指令为空时的回退来源 | [style/packs.py](../../src/jshi/style/packs.py) · `SUXIPO_INSTRUCTION` / `SUXIPO_BLOCK` |

### P30 · 主认知材料与统一组装（预留 P30-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P30-10 | system 组装模板 | 当前时间、认知指令、承诺、边界和附加规则合并 | [models/prompt.py](../../src/jshi/models/prompt.py) · `build_system` |
| P30-20 | 默认 user 组装模板 | 说话人、活跃区、JEV参考、回忆、人物材料、工具材料与本轮输入 | [models/prompt.py](../../src/jshi/models/prompt.py) · `_build_user_body` |
| P30-30 | 人格 user 组装模板 | 人格回应使用的片场、输入、回忆、人物参考及工具材料 | [subject/process.py](../../src/jshi/subject/process.py) · `_persona_user_text` |
| P30-40 | 人物与事实参考说明 | 姓名未明、档案暂定、同名歧义、人物肖像、相处经验、JEV参考及回忆不可用时的提示 | [models/prompt.py](../../src/jshi/models/prompt.py)、[subject/process.py](../../src/jshi/subject/process.py) · 动态片段，随对应输入模板完善 |
| P30-50 | 本轮输入与播放事实模板 | 表达本批输入、闲时刺激与实际播出状态，供模型分清输入和交付事实 | [core/main_input.py](../../src/jshi/core/main_input.py)、[models/prompt.py](../../src/jshi/models/prompt.py)、[subject/process.py](../../src/jshi/subject/process.py) · `render_inputs` / `format_turn_input` / `transport_context` |
| P30-60 | 超级权限附加规则 | 用户写入规则后注入提示词 | [privilege/rules.py](../../src/jshi/privilege/rules.py)、[subject/process.py](../../src/jshi/subject/process.py) · 规则存储与格式化；内容由用户提供 |
| P30-70 | 通用 JSON 输出约束与 Schema 拼接 | 各 Skill 的最终 system 尾部，限定输出格式 | [skill/base.py](../../src/jshi/skill/base.py) · `Skill.system_extra`；各调用另有专用 Schema |
| P30-90 | 非认知请求的简短 system 回退 | 无主认知专用指令时组装身份与当前立场 | [models/prompt.py](../../src/jshi/models/prompt.py) · `_short_system` |

### P40 · 工具策划、执行与反馈（预留 P40-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P40-10 | 工具策划 | 主认知决定需要工具后，将 need 转成可执行定义或放弃理由 | [skill/tool_plan.py](../../src/jshi/skill/tool_plan.py) · `ToolPlanSkill.instruction` |
| P40-20 | Pi 单次工具执行 | 将工具、需求、参数、现场引用与反馈要求交给执行引擎 | [tool/pi_engine.py](../../src/jshi/tool/pi_engine.py) · `_build_prompt` 普通分支 |
| P40-30 | Pi 多步骤任务执行 | 按建议步骤和依赖执行工具任务 | [tool/pi_engine.py](../../src/jshi/tool/pi_engine.py) · `_build_agent_loop_prompt` |
| P40-40 | Pi 创建工具 | 创建可重复调用的工具及配套技能文档 | [tool/pi_engine.py](../../src/jshi/tool/pi_engine.py) · `_build_create_prompt` |
| P40-50 | Pi 技能调用参数模板 | 通过 `/skill:<name>` 将本轮需求和参数交给技能 | [tool/pi_engine.py](../../src/jshi/tool/pi_engine.py) · `_skill_slash_command`；技能正文来自实际安装的技能 |
| P40-60 | 工具策划反馈包装 | 将策划阶段结果转为给主认知看的事实 | [skill/tool_wrap.py](../../src/jshi/skill/tool_wrap.py) · `PLANNING_PROMPT` |
| P40-70 | 工具进展反馈包装 | 将执行中的阶段进展转为给主认知看的事实 | [skill/tool_wrap.py](../../src/jshi/skill/tool_wrap.py) · `PROGRESS_PROMPT` |
| P40-80 | 工具最终结果包装 | 将最终结果转为给主认知看的事实 | [skill/tool_wrap.py](../../src/jshi/skill/tool_wrap.py) · `RESULT_PROMPT` |
| P40-90 | 主认知的工具相关材料模板 | 把工具状态、结果及续办材料放回后续主认知 | [tool/service.py](../../src/jshi/tool/service.py)、[subject/process.py](../../src/jshi/subject/process.py) · 动态输入材料 |

### P50 · 片场初始化与写场（预留 P50-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P50-10 | 斯密斯：初始化片场 | 非木头人格无片场时的一次性写场景；阿丘复用 | [style/packs.py](../../src/jshi/style/packs.py) · `SMITH_BOOT_INSTRUCTION` |
| P50-20 | 苏西坡：初始化片场 | 苏西坡无片场时的一次性写场景 | [style/packs.py](../../src/jshi/style/packs.py) · `SUXIPO_BOOT_INSTRUCTION` |
| P50-30 | 初始化素材 user 模板 | 汇集近期活跃区、回忆、人物材料和价值供初始化使用 | [subject/process.py](../../src/jshi/subject/process.py) · `_boot_user_text` |
| P50-40 | 木头：写场 | 回应后维护默认活跃区 | [skill/zone.py](../../src/jshi/skill/zone.py) · `WOOD_WRITE_INSTRUCTION` |
| P50-50 | 斯密斯：写场 | 回应后修改片场；阿丘复用 | [style/packs.py](../../src/jshi/style/packs.py) · `SMITH_WRITE_INSTRUCTION` |
| P50-60 | 苏西坡：写场 | 回应后按苏西坡文风修改片场 | [style/packs.py](../../src/jshi/style/packs.py) · `SUXIPO_WRITE_INSTRUCTION` |
| P50-70 | 写场 user 与本轮回应材料模板 | 提供旧片场、原话、回应、未开口事项与新回忆；人格分支追加回应材料 | [subject/process.py](../../src/jshi/subject/process.py) · `_wood_write_user_text` 与写场请求组装；人格基础模板见 P30-30 |

### P60 · 后台 JEV 情境与人物复核（预留 P60-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P60-10 | [第二次 JEV：完整情境整理](P60-10-第二次JEV.md) | 后台整理 JEV 片场，为后续入口辨人和主认知交往判断提供参考 | [voice/jev_prompts.py](../../src/jshi/voice/jev_prompts.py) · `SCENE_INSTRUCTION`；已完善，1 个完整实际例子，固定正文与当前版一致；2026-10-10 |
| P60-20 | [第二次 JEV 输入模板与输出约束](P60-10-第二次JEV.md#最终请求如何组装) | 组装事件快照、主片场和情境输出 Schema | [voice/jev_scene.py](../../src/jshi/voice/jev_scene.py) · `JEVSceneWriter`；已并入 P60-10 完善，共用其 1 个实际例子；2026-10-10 |
| P60-70 | 独立人物复核指令 | 单独的 `PersonReviewer` 实现，为后续 JEV 提供建议；当前语音后台接的是 `JEVSceneWriter` | [voice/person_review.py](../../src/jshi/voice/person_review.py) · `PERSON_REVIEW_INSTRUCTION`；保留实现，区别于当前后台入口 |
| P60-80 | 共用人物复判片段 | 人物建议的旧/兼容片段；本次检索未发现其他模块引用此常量 | [core/conversation_review.py](../../src/jshi/core/conversation_review.py) · `REVIEW_INSTRUCTION` |

### P70 · 人物肖像与记忆整理（预留 P70-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P70-10 | 人物肖像三档浓缩 | 将已有白描肖像浓缩为 L1 / L2 / L3，供不同主认知档使用 | [memory/portrait_views.py](../../src/jshi/memory/portrait_views.py) · `PortraitViewStore.refresh` |
| P70-20 | 肖像浓缩不合约重试 | 输出超限或不合约时追加字符数与重新浓缩要求 | [memory/portrait_views.py](../../src/jshi/memory/portrait_views.py) · `refresh` 重试消息 |

REMS 人物白描、相处经验和记忆加工通过外部 REMS 实现接入，提示词正文不在本项目 `src/jshi` 中。后续需要完善这部分时，再核查实际使用的 REMS 源码或配置，列入本组空闲编号；不把本地肖像浓缩指令当作其生成提示词。Pi 外部技能同样按实际使用的技能补充。

### P80 · 自省与记忆回顾（预留 P80-01～99）

| 编号 | 提示词 / 模板 | 用途与流程位置 | 代码入口 / 备注 |
| --- | --- | --- | --- |
| P80-10 | 自省 | 活动间隙回看自身行为、当前个人世界与场景材料 | [skill/introspection.py](../../src/jshi/skill/introspection.py) · `INTROSPECTION_INSTRUCTION` |
| P80-20 | 自省输入材料模板 | 为自省调用组装触发原因与现场材料 | [reflection/inprocess.py](../../src/jshi/reflection/inprocess.py) · `_model_request` |
| P80-30 | 回忆评价 | 以当时问题和后续交流评价一次回忆 | [reflection/thoughts.py](../../src/jshi/reflection/thoughts.py) · `RecallEvaluationThought.instruction` |
| P80-40 | 旧经历再加工 | 回顾旧经历，发现关系或修正理解 | [reflection/thoughts.py](../../src/jshi/reflection/thoughts.py) · `RecallReprocessingThought.instruction` |
| P80-50 | 压力容器比较 | 按当前情境比较样例的相对保留偏好 | [reflection/thoughts.py](../../src/jshi/reflection/thoughts.py) · `PressureComparisonThought.instruction` |
| P80-80 | 手动反思输入模板 | 手动 `reflect` 入口拼接触发文本与近期历史 | [subject/process.py](../../src/jshi/subject/process.py) · `_reflect_internal`；区别于队列自省 |

### P90 · 共用提示词片段（预留 P90-01～99）

这些片段随完整提示词拼装，不分别发起模型调用。具体完善时可在单项文档中引用，避免多处重复维护全文。

| 编号 | 片段 | 用途 / 定义位置 |
| --- | --- | --- |
| P90-10 | 身份与基本约束 | [style/packs.py](../../src/jshi/style/packs.py) · `COMMON_CORE` |
| P90-15 | 斯密斯固定价值叙述 | [style/packs.py](../../src/jshi/style/packs.py) · `SMITH_VALUE_NARRATION`；程序直接放入片场，不由模型生成 |
| P90-20 | 交往参与与接话 | [style/prompt_levels.py](../../src/jshi/style/prompt_levels.py) · `DIALOGUE_DIRECTION_NOTE` |
| P90-25 | 回应方式与未开口事项 | [style/packs.py](../../src/jshi/style/packs.py) · `RESPONSE_MODE_BLOCK` |
| P90-30 | 片场、回忆与时间含义 | [style/packs.py](../../src/jshi/style/packs.py) · `PERSONA_SITUATION_NOTE` |
| P90-35 | 来源优先级 | [style/packs.py](../../src/jshi/style/packs.py) · `SOURCE_PRIORITY_NOTE` |
| P90-40 | 工具任务规则 | [style/packs.py](../../src/jshi/style/packs.py) · `TOOL_TASK_NOTE`；`PERSONA_TOOL_NOTE` 为同一内容别名 |
| P90-45 | 工具回应材料使用规则 | [style/packs.py](../../src/jshi/style/packs.py) · `TOOL_REPLY_NOTE` |
| P90-50 | 人格输出规则 | [style/packs.py](../../src/jshi/style/packs.py) · `PERSONA_OUTPUT_RULES` |
| P90-55 | 人格底本写场规则 | [style/packs.py](../../src/jshi/style/packs.py) · `ZONE_WRITE_RULES` |
| P90-60 | 人格底本编辑规则 | [style/packs.py](../../src/jshi/style/packs.py) · `EDIT_RULES` |
| P90-65 | 独立写场材料说明 | [style/packs.py](../../src/jshi/style/packs.py) · `WRITE_MATERIAL_NOTE` |
| P90-70 | 独立写场编辑规则 | [style/packs.py](../../src/jshi/style/packs.py) · `WRITE_EDIT_RULES` |
| P90-75 | 片场块重要性分档 | [style/packs.py](../../src/jshi/style/packs.py) · `SCENE_TIER_NOTE` |
| P90-80 | 片场与工具预算说明 | [style/packs.py](../../src/jshi/style/packs.py) · `format_zone_budget_note` |

P00、P15、P25 等未使用的分组可留给新增模块。现有组内空号均可插入新条目，不必填占位行。

## 每份提示词文档的内容

1. **说明**：用途、使用场景，以及对应的代码或配置位置。
2. **流程位置**：在哪一步调用，上游提供什么输入，下游使用什么输出。
3. **提示词全文**：保留完整内容；有变量、动态拼接或不同分支时，说明其来源和组合方式，不用摘要替代全文。
4. **实际例子（1–2 个）**：保留实际输入或上下文、填入实际内容后的完整提示词，以及已有的实际输出。暂时没有真实例子时标记“待补充”，等有了再添加，不编造实际案例。
5. **更新记录**：简短记录日期、改动内容和原因。

## 维护约定

- 索引先列全项目内可确认的提示词；用户指定某条后，再新增或完善对应的具体文档，并在索引添加文档链接、完善状态、例子数量和更新日期。
- 每次修改提示词，同步更新受影响的索引条目；已有具体文档时，同时核对并更新用途、流程位置、全文和受影响的例子。
- 更新以现有文档为基础，只修改受影响的部分；不每次重新生成整份文档或整个目录。
- 索引中只有受影响的条目需要更新；编号和已有链接保持稳定。
- 发现新增提示词时，按模块和流程补入索引的空闲编号；不自动批量生成全文文档，待用户指定后再完善。
- 实际例子不足时允许暂缺，并明确标记，后续获得实际材料时补齐。

创建日期：2026-10-10。

更新记录：2026-10-10，根据用户要求列出项目提示词及组装片段，改用分组留空编号；单项文档仍按需完善。

2026-10-10：按用户要求完善第一次与第二次 JEV，分别建立 P10-30、P60-10 文档；配套 P10-40、P60-20 同文维护，各收录 1 个完整实际调用例子。
