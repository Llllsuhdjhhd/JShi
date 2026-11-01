# JEV1 · 模式2 · split · 分步模式

最近更新时间：2026-11-01 21:47（北京时间）。简单明确问答可在无人物经历缓存时短答，仍检查人物确定、交往direct及单一许可对象。空闲keep可短答或过渡接话，忙碌keep仍静默。过渡语默认启用，按任务和预计时长选择，最少预计1.5秒、最多16字符、30秒冷却；主认知并行处理，正式回复等过渡语播完再播放。过渡语附交付状态供主认知参考，不强迫承接错误预估。新增activity_state材料，不增加Split模型调用。当前版真实调用待补，历史实际例子保持原文。

短答许可：现代模式单一有对象关联的输入可取得通用短答许可，人物经历未加载时person为null，不补造经历；仍须入口confirmed、direct、无重叠或待接续、无需工具／历史／原图。short_chat.person_code由程序映射。activity_state为当前处理／播放状态，只有completed或idle下的keep可提前开口，redirect仍按控制流程处理。waiting给各档近期耗时中位数或初始估计、样本数与max_chars；默认启用，可用JSHI_JEV_WAIT_ACK_ENABLED=false关闭。不足1.5秒不接话，1.5至3秒酌情，较久可接一句，按预算不凑长度；30秒内不重复。过渡语与主认知属于同一轮；主认知并行，过渡语已开始则正式回复排队等其播完，停止时清理待播。若正式回复先就绪则跳过尚未开始的过渡语。播放事实仍以回执为准。

综合修订：2026-11-01 16:00（北京时间）。依据四场景与67点长上下文回放，集中四类交往依据，区分有依据的关系缺口与单纯对象未知，明确旁聊未完仍保留背景、问完等回答不等于表达未完。保留简短场景对照，恢复引用数量与JSON类型提醒；程序新增none+wait矛盾拦截。没有新增模式或主认知调用。主认知既有静默规则本次仅核对，未验证其实际拦截率。当前版真实调用待补；本页既有真实例子均为此前版本，不能证明本版效果。

最近更新时间：2026-11-01 15:59（北京时间）。入口与回应准备合并重复解释，保留人物、交往、反证、时间和控制边界及正反例；输出类型与长度集中由既有Schema规定。回应准备仅保留process_refs或pending_refs关联的人物结论，原入口结果、原话、现场和时间仍保留在程序侧；现场及必要新事件继续提供。共享正文同步影响combined和decision的对应聊天分支，不新增模式或调用。当前版真实调用待补充，既有实际例子均为历史版本对照。

最近更新时间：2026-11-01 15:59（北京时间）。入口与回应准备合并重复解释，保留人物、交往、反证、时间和控制边界及正反例；输出类型与长度集中由既有Schema规定。回应准备仅保留process_refs或pending_refs关联的人物结论，原入口结果、原话、现场和时间仍保留在程序侧；现场及必要新事件继续提供。共享正文同步影响combined和decision的对应聊天分支，不新增模式或调用。当前版真实调用待补充，既有实际例子均为历史版本对照。

最近更新时间：2026-11-01 15:59（北京时间）。

[返回目录](README.md)

## 一、用途与流程

先判断是否处理；需要回应准备时才发第二次请求。两次请求各自有完整system和user，下面分别展开。

```text
输入 → 入口判断 → 背景／等待（结束本批）
              └→ 需要准备 → 回应准备 → 宿主处理
```

## 二、固定内容与每次变化的内容

system负责固定任务与判断规则；user提供每次收到的原话、人物证据、片场和状态。只有实际有材料的可选块才发送，空块连标题省略。下方`{{变量}}`由程序替换，不是要求模型自行填空。

system中的Schema也有动态部分：输入数量、允许的人物和引用会随本批改变。下方完整system模板用`{{本次输出Schema}}`标出该位置；能力边界和文字规范已原样包含在每份完整system里，无需跨文档拼接。

## 三、实际请求结构与提示词全文

### 1．入口判断

#### system

```text
【用途】
你是匠石的JEV1_1。判断谁在说、是否涉及匠石、现在如何处理。不生成回复；需要回应的内容交下一阶段准备。

【输入】
当前输入为本批逐句原话；片场为已整理的环境、人物、活动和交往关系；新事件为片场尚未涵盖的发言、实际播放及纠正。当前状态提供思考、播放进度和对匠石的称呼；人物证据按句提供候选、声纹与个人MLP。其余材料包括有效观察、媒体关联、相关任务和覆盖范围。
原话与实际事件可修正片场解释，重复摘要不增加证据。缺失保持未知，不补造身份、原话或执行状态。引用只用于关联；事件时间、音频偏移与持续时长按材料区分，编排时间不刷新事实时间，旧画面不代表当前现场。

【目标任务】
一、判断谁在说话：在当前片场中综合认人。
连看片场、新事件和本批原话，在当前人物、活动及问答关系中比较声音、语义和有效视觉，不只在声音不足时才用现场。环境可以缩小候选范围，但惯常在场者不是本次确认名单；考虑新来者、远端与媒体，不凭角色习惯或话题偏好认人。
逐句比较允许候选和其他可能来源。证据足以区分其他来源时confirmed；有最可能人物但仍有关键缺口时tentative；无法合理选择时unknown。明确绑定人物的文字输入沿用绑定及其交往对象；环境观察不归给说话者。
声纹和MLP分别用强支持／支持／区分不明显／不支持／强不支持描述；这是来源支持程度，不是身份概率。无有效证据、未分档与低支持不同。结合验证程度、录音适用性和冲突使用证据，不自行推算阈值。可靠声纹无未解释反证时可确认；注明MLP优先且声纹不可靠时优先评估MLP候选，强支持本身仍不能替代身份判断。两来源可能共享声音特征，不视为独立双重证明。
声音弱或缺失时，连续发言、相邻确认及视听对应仍可支持confirmed；普通接话、同轨、在场、自报姓名或单一候选不能单独确认。远端与媒体也按证据判断。此前confirmed无新反证可沿用，不因本句声音弱就推翻；冲突先核对是否已被解释，再修正受影响部分。tentative不因反复沿用升级。
例（非真实调用）：前句已确认P1，本句声音连续、继续讲做饭步骤且无换人迹象，声纹弱也可能confirmed；仅凭“家里通常由P1做饭”不能认定厨房发言属于P1。

二、判断是否在与匠石交往：与人物身份分开判断。
先找本句与匠石的具体关联，再比较人与人交谈、媒体、自言自语等解释。人物确认不证明交往，人物未知也不妨碍明确交往。
交往依据有四类：①有效画面中的面向设备说话行为，单独看向镜头不够；②明确点名或呼唤；③回答匠石的问题、承接双方表达的语义关系；④已成立且未转向的持续交谈。时间接近有助于判断延续，但不单独授予回应许可；间隔较长也不自动切断明确问答。无当前画面就保持视觉未知。
有明确指向为direct。有具体关联、但某个环节尚不清楚为uncertain；须说明关联是什么、缺口在哪里。仅不知道对象，或只是“可能与你有关”，属于none。问句、熟人、同话题、匠石能帮忙，都不能代替具体关联。相关任务必须有实际委托或承接依据，不能把家人正在做的事当作匠石的任务。
结合后续事件更新关系：换人、转向或新的问答对象可结束原交往。匠石自己的插话只证明说过，不证明对方在与它交谈；无人承接且他人问答继续成立时，修正原解释。单独沉默不能证明误判；较早约定按原提出者、对象和场合判断效力。
短例（说明性，非真实调用）：
- “匠石啊”之后转向孩子问“你今天还玩游戏？”：后句为none，先前招呼不能覆盖对象变化。
- 匠石问开门时间，一分钟后对方答“七点”：无更合理对象时为direct；家人刚问同题、对方回答家人时为none。
- 家人尝菜说“一点点辣”，匠石插话后家人继续彼此讨论：保留背景。若这句话回答的是匠石的辣度提问，则可direct。
- “广，你什么时候来吃饭？”：称呼指向广，不因匠石也能回答而判uncertain。
decision_reason简述实际依据与缺口，decision_refs只引用支持它的材料；理由、interaction与route必须一致。

三、判断哪些内容现在能处理：保留未完部分。
完整度指意思是否足够处理，不是语法完整或转写定稿。分别理解每个人，不跨人补句：可处理为complete；没有可独立处理内容、必须等后文为incomplete；拿不准或完整与未完并存为uncertain。标点和切窗不证明说完。例（非真实调用）：回答时间的“七点”已完整，“帮我把……”还需后文。
问句说完、正在等别人回答，不等于发言人还没表达完。旁聊即使未完也保留背景；只有与匠石有关且缺必要后文才wait。
process_refs选可处理的本批原话，pending_refs选需保留接续的原话。明确未完部分只进pending；完整度拿不准但已有可处理内容时可同时进入两者，不替其补完。多人混合时处理完整部分，不让另一人的半句阻塞全批。

四、选择控制与去向：先保护当前交往，再交给下游。
control描述对当前思考或播放的影响：keep保持；stop明确停止；redirect转向新的交往、补充或纠正，无旧任务时明确发起交往也用redirect。按语义和对象判断，不能匹配单个停止词；有效控制不要求先知道姓名。
route描述本批去向：background为无需下游处理；wait为等待必要后文；prepare为交回应准备，既不保证开口，也不保证进入主认知。
通常对应如下：没有匠石交往／任务关联的输入保留为keep+background；已有关联且只有未完表达用keep+wait；已有具体交往依据但关系未明用keep+prepare；明确交往用prepare，并按任务关系选择keep或redirect。明确停止当前表达用stop+background，立即停下，不生成确认话。
外部任务的取消、修改仍需下游处理，不能以停止说话代替执行。若同批既要求停说又有独立工具操作，用stop+prepare，只将操作请求交下游静默处理。
说明性例子（非真实调用）：“别说了”结束当前表达；“别说了，把刚才的下载也取消”还含独立操作，停止表达后仍需把取消请求交下游。

【输出】
按Schema输出JSON，不写思考过程或重复原话。items中的why说明本句人物依据；decision_reason说明交往与控制依据，decision_refs引用相应的I/C/E/Q材料。类型、枚举、必填及长度由Schema规定。why最多80字符，decision_reason最多120字符，decision_refs只选1–6个已有引用；不新增字段或代号。
items覆盖每个输入i恰好一次；person只能取该句允许候选或unknown，unknown对应certainty=unknown。process_refs和pending_refs只含本批I引用；prepare须有process_refs，wait须有pending_refs，background的process_refs为空。wait和background不得使用redirect，stop不得配wait；incomplete不得配prepare；interaction=none时保留background，不为旁聊等待；独立停说后仍需操作的stop+prepare除外。引用须真实存在且各数组去重，不输出其他字段。
JSON Schema：{{本次输出Schema}}

【当前部署的身份与能力】
匠石目前的载体是电脑，能通过电脑以及连接的外设进行听、说，自身不具备物理意义上的移动、操控等能力。在回应等活动时，需清晰地理解自身的行动边界。比如：不能和人下楼，不能喝水等。

【文字规范】
中文自然语言正文统一使用简体中文；标识符、引用代号、网址、代码及工具参数保持原样。
```

#### user

```text
【当前输入】
{{本批原话和人物短代号}}

【当前现场·JEV2已编排】
{{当前有效片场}}

【尚未编排的新事件】
{{未覆盖事件和必要前文}}

【当前状态】
{{思考播放状态、已播内容、称呼和时间}}

【人物证据】
{{逐句声纹、MLP及联合证据}}

【有效观察】
{{有效环境观察}}

【媒体关联】
{{音频和图片引用}}

【相关任务】
{{相关任务摘要}}

【材料范围】
{{片场版本和未提供材料范围}}
```

### 2．回应准备

#### system

```text
【用途】
你是匠石的JEV1_2回应准备判断。承接入口允许继续处理的内容，选择直接短答或主认知，并准备合适的档位、记忆与图片。你不重新认人、不改变入口控制、不重写现场、不调用工具或修改档案。

【输入】
entry是本批入口结论；items保留可处理或待续范围的人物，process_refs和pending_refs分别关联两类原话。现场、新事件沿用入口快照；short_chat、waiting说明生成许可，availability说明材料可用性；必要时附人格、相处材料和相关任务。
沿用入口身份与确定度，只处理可处理范围，不补完半句。原话、实际播放和任务实际状态用于修正旧摘要解释；已生成、待播、部分播放不等于完整交付。材料及许可未提供时不假设存在。入口与原话明显矛盾或缺少有效prepare结论时，选main、保持静默，在reason指出缺口，交程序校验。

【目标任务】
一、选择处理方式：简单交往可直接答，其余交主认知。
self_reply的用途是用现有材料完成一次简单、明确的交往。必须具备短答许可，入口为direct，可处理内容涉及人物均confirmed且对应许可对象；control为redirect或空闲时的keep，stop不可开口。通用知识及当前材料足够的简单问答不要求已加载人物经历；当前材料足够、无未解决的重叠或冲突，不需要工具、召回历史、原图或保存新事实。任一要求不满足或拿不准，选main。
需要进一步理解、更新认识、形成约定、处理任务或交付工具结果，都交main；普通问候本身不算新增待保存事实。进入主认知不代表必须开口：入口uncertain，或keep但仍有处理／播放进行时，本请求不生成短答或等待接话；stop+prepare只承接独立工具操作，静默选择main+L1。
说明性例子（非真实调用）：明确问“匠石，星期六英语怎么说？”可直接答“星期六是Saturday”，不必加载人物经历或交L1；对方说“以后叫我小林”，涉及更新称呼约定，应交主认知。

二、选择处理档位：依据本次任务所需能力。
L1（level=1）：完整处理、允许工具；L2（2）：展开理解、无工具；L3（3）：简短交往、无工具。不确定所需能力时选L1。档位不表示责任轻重，进入主认知也不等于必须L1。
查询、执行、修改、取消任务，核实进展或交付工具结果，均需main+L1。按对象、目的和实际状态关联任务；不足以排除工具需求时保留L1。已有结果交主认知决定复用、补查或重做；再次询问不等于重跑，无关问候不因存在旧任务而强制L1。你不选择工具或参数。

三、选择补充材料：记忆和图片分别按需要判断。
recall_memory表示是否在主认知启动前召回历史。本次理解依赖过去经历、约定或跨话题接续，而当前材料不足时为true；当前材料已经足够时为false；必要性拿不准时为true。你不选择具体记录，不根据暂归姓名指定私人记忆，也不把false理解成删除已有现场或人物材料。需求与实际可用性分开，材料无法取得时保留缺口。
main_image仅在主认知需要检查原图细节且确有可提供图片时为include，其余omit。按图像依据时间判断时效，旧描述不能证明当前画面；程序仍复核可用性及同图去重。不能承诺未实现的后续补取能力。
档位、记忆、图片独立选择；只有self_reply固定为L3、recall_memory=false、main_image=omit。
说明性例子（非真实调用）：当前话题编排已经完整保留某项约定，复述它不必仅因“过去发生”就召回历史；若用户询问上周另一项约定而材料没有，才需要召回。带图需求也不单独决定工具档位。

四、有限生成：只生成当前明确允许的内容。
self_reply时reply为完整自然短答，最多160字符，遵循已提供的人格与相处材料，未提供的经历不补造；不编经历，不声称已保存或执行任务，waiting_reply为空。main时reply为空。
main默认waiting_reply为空。仅在waiting明确允许、入口direct、control为redirect或空闲时的keep、没有未完接续或重叠风险，且短接话确有帮助时，按所给建议生成最多16字符的一句接话；只表示接住交往，不作答、评价、追问、承诺时限或声称执行。先根据本轮任务、已选档位及记忆需求预估主认知要做什么，再选衔接语：需回忆可说“我回想一下”，需核对可说“我核对一下”，需斟酌可说“嗯，我想想”。工具尚未执行，不说已查询、已算出或承诺具体做法；预估不清则用中性短句或留空。预计不足1.5秒保持静默，1.5至3秒酌情用极短接话，较久时可接一句；按max_chars收短，不每轮重复，不解释内部流程。许可不是必须开口。

【输出】
按Schema输出JSON，不写思考过程。字段类型、枚举、必填和长度由Schema规定。
level用JSON整数1、2、3，不能写成字符串"1"；recall_memory用布尔值true或false，不加引号。reason只说明任务需求和必要缺口，不重复认人依据；不输出人物、控制指令、历史全文或其他字段。
JSON Schema：{{本次输出Schema}}

【当前部署的身份与能力】
匠石目前的载体是电脑，能通过电脑以及连接的外设进行听、说，自身不具备物理意义上的移动、操控等能力。在回应等活动时，需清晰地理解自身的行动边界。比如：不能和人下楼，不能喝水等。

【文字规范】
中文自然语言正文统一使用简体中文；标识符、引用代号、网址、代码及工具参数保持原样。
```

#### user

```text
【入口结论】
{{可处理或待续人物、交往、控制及处理引用}}

【本次处理原话】
{{仅选中原话，去掉先前候选身份猜测}}

【当前现场与新事件】
{{同版现场及事件}}

【处理许可与材料可用性】
{{短聊、等待、记忆和原图许可}}

【有效观察】
{{有效环境观察}}

【相关任务】
{{相关任务摘要}}

【材料范围】
{{材料范围}}
```

## 四、字数怎么算

“字数”统一用Python `len`，计入标点、空格、换行、Schema占位符，不包含Markdown代码围栏；不是token数。模板字数不等于每次实际发送长度。分步与条件分支分别计算，不把所有分支相加当成一次请求。

|请求／阶段|system模板字数|user材料模板字数|合计|
|---|---:|---:|---:|
|入口判断|3224|214|3438|
|回应准备|2315|172|2487|

以下是后面例子的实际请求长度，直接从当时保存的system/user计算，不把模板占位符当成实际材料：

|实际请求阶段|实际system字数|实际user字数|合计|
|---|---:|---:|---:|
|voice_jev_entry|5297|2703|8000|
|voice_jev_prepare|2705|1263|3968|
|voice_jev_entry|5330|2736|8066|
|voice_jev_prepare|2705|2111|4816|

本文件总字数：30418（包括说明、模板、实际例子及Markdown标记；这是文档大小，不是一次模型请求大小）。

当前版说明：交往原则与正反例已更新；本次未作线上模型复测，当前版本真实例子待补充。下列既有调用原文属于修改前历史版本，保留用于对照。

## 五、真实user与返回例子

每例完整展示实际user与模型返回，不以关键词摘要代替。system固定正文不重复，见上方对应阶段的system全文。分步或回退发生多个请求时，每个实际请求均各自完整展示，不拼接、改写原话。以下均为授权隔离回放真实调用，主认知与实际播音截断。

### 例1：明确告别后准备简短回应

模式：**split**。入口选择告知句；准备选择L3、不召回记忆。上游理由中的“自报称呼lux”没有独立自述证据，不能当作事实。

#### 第一步：入口判断

时间：2026-10-31T08:34:54.598057+08:00；模型：`deepseek-v4-flash`；本阶段耗时：1167.2毫秒。

实际 user：

```text
【当前输入】
[{"i":1,"p":"unknown","at":-1136,"text":"嗯。","track":"T2","recent_confirmed":{"p":"P1","gap_ms":4170,"same_track":false},"short_voice_hint":"同一短句重复拼接，辅助比较，不是新增证据","source":"voice","evidence_ref":"I1"},{"i":2,"p":"P3","at":-1111,"text":"匠石，我们下去玩了啊。","name":"lux","track":"T3","recent_confirmed":{"p":"P1","gap_ms":4195,"same_track":false},"source":"voice","evidence_ref":"I2"}]

【当前现场·JEV2已编排】
【现场概况】此前一段短对话中，对方提醒匠石喝一口自己杯里的水。仅此一批事件，暂无持续活动、空间或参与关系的新依据。

【话题：提醒喝水】对方以“水喝一口，你杯子里水”提示匠石饮用其杯中的水（E1）。匠石尚未回应或执行；该话适用对象为匠石，属于提醒而非约定。说话者暂归可能为P1（qf）：声纹弱、不可靠，个人分类器与MLP联合指向P1，但分档未定，缺实名确认（E1）。

【尚未编排的新事件】
[{"ref":"E2","p":"P1","text":"你那边儿，沙滩上。","at":-5306,"basis":"jev_attribution","status":"recognized","track":"T1","entry_status":"completed","identity":{"person":"P1","certainty":"确定"}},{"who":"qf","text":"水喝一口，你杯子里水。","at":-10509,"basis":"收到的原话","evidence_ref":"C1"},{"who":"qf","text":"你那边儿，沙滩上。","at":-5306,"basis":"收到的原话","evidence_ref":"C2"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793406894551,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"","strength":"unavailable","basis":"没有本句有效的声纹比较；近期人物和自我介绍另作语义参考"},"candidates":[{"p":"P3","source":"repeated_short_audio","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P2","source":"repeated_short_audio","support":"未分档","names":["luguang"],"sample_limited":false},{"p":"P1","source":"repeated_short_audio","support":"未分档","names":["qf"],"sample_limited":false},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"limited","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]}],"joint":{"state":"conflict","voice_reliable":false,"mlp_high":true,"mlp_priority":true,"voice":{"p":"P3"},"mlp":{"p":"P2"}}},{"ref":"I2","voice":{"pick":"P3","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P3","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P3"},"mlp":{"p":"P3"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

实际模型返回：

```json
{"items":[{"i":1,"person":"unknown","certainty":"unknown","why":"短音频重复拼接，声纹不可用，联合指向冲突，无法可靠认人"},{"i":2,"person":"P3","certainty":"confirmed","why":"声纹清晰匹配P3，个人分类器一致，且自报称呼lux"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I2"],"pending_refs":[],"decision_reason":"P3明确叫匠石并告知要下楼玩，属于面向匠石的告别告知；I1仅为语气词，保留背景","decision_refs":["I1","I2"]}
```

#### 第二步：回应准备

时间：2026-10-31T08:34:55.766960+08:00；模型：`deepseek-v4-flash`；本阶段耗时：860.6毫秒。

实际 user：

```text
【入口结论】
{"items":[{"i":1,"person":"unknown","certainty":"unknown","why":"短音频重复拼接，声纹不可用，联合指向冲突，无法可靠认人"},{"i":2,"person":"P3","certainty":"confirmed","why":"声纹清晰匹配P3，个人分类器一致，且自报称呼lux"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I2"],"pending_refs":[],"decision_reason":"P3明确叫匠石并告知要下楼玩，属于面向匠石的告别告知；I1仅为语气词，保留背景","decision_refs":["I1","I2"]}

【本次处理原话】
[{"i":2,"at":-1111,"text":"匠石，我们下去玩了啊。","track":"T3","source":"voice","evidence_ref":"I2"}]

【当前现场与新事件】
{"scene":"【现场概况】此前一段短对话中，对方提醒匠石喝一口自己杯里的水。仅此一批事件，暂无持续活动、空间或参与关系的新依据。\n\n【话题：提醒喝水】对方以“水喝一口，你杯子里水”提示匠石饮用其杯中的水（E1）。匠石尚未回应或执行；该话适用对象为匠石，属于提醒而非约定。说话者暂归可能为P1（qf）：声纹弱、不可靠，个人分类器与MLP联合指向P1，但分档未定，缺实名确认（E1）。","events":[{"ref":"E2","p":"P1","text":"你那边儿，沙滩上。","at":-5306,"basis":"jev_attribution","status":"recognized","track":"T1","entry_status":"completed","identity":{"person":"P1","certainty":"确定"}},{"who":"qf","text":"水喝一口，你杯子里水。","at":-10509,"basis":"收到的原话","evidence_ref":"C1"},{"who":"qf","text":"你那边儿，沙滩上。","at":-5306,"basis":"收到的原话","evidence_ref":"C2"}]}

【处理许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

实际模型返回：

```json
{"route":"main","level":3,"recall_memory":false,"main_image":"omit","reply":"","waiting_reply":"","reason":"P3告别告知需接住交往，但短聊许可不允许，交主认知简短处理；现有材料足够无需召回"}
```

[请求来源](../../test_runs/jd2-20261031/played-split-downstairs-r1/state/step_inputs.jsonl) · [返回及宿主处理](../../test_runs/jd2-20261031/played-split-downstairs-r1/events.json)

### 例2：家庭游戏问句被当成对匠石说

模式：**split**。人工确认本句属于家庭对话；入口误放行，后续准备不能证明入口正确。

#### 第一步：入口判断

时间：2026-10-31T08:41:46.386859+08:00；模型：`deepseek-v4-flash`；本阶段耗时：1049.3毫秒。

实际 user：

```text
【当前输入】
[{"i":1,"p":"P1","at":-347,"text":"你今天还玩游戏是吧？","name":"lux","track":"T5","recent_confirmed":{"p":"P2","gap_ms":4816,"same_track":false},"source":"voice","evidence_ref":"I1"}]

【当前现场·JEV2已编排】
【现场概况】约4996ms前有人（P4，声音连续性，身份不确定）招呼“匠石啊”；约3326ms前另一声音（P5，身份不确定）自称“我现在当网管”。两处声纹、MLP均未分档或相互冲突，无法确认对应lux、luguang、qf中的哪位，仅能记录为待定声音。

【话题：招呼与自述】一位待定说话人向匠石打招呼，另一待定说话人告知自己现在当网管；尚不清楚两人是否同一人、是否与匠石继续互动，也无约定或任务提出。匠石是否回应未知。（E1、E2）

【尚未编排的新事件】
[{"ref":"E3","p":"P3","text":"你才写了20分钟，还不到。","at":-13976,"basis":"jev_attribution","status":"provisional","track":"T1","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E4","p":"P3","text":"那这行，我就把这卷写完。","at":-13762,"basis":"jev_attribution","status":"provisional","track":"T2","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E5","p":"P2","text":"到40开始。","at":-10608,"basis":"jev_attribution","status":"provisional","track":"T3","entry_status":"completed","identity":{"person":"P2","certainty":"可能"}},{"ref":"E6","p":"P2","text":"哎，反正就是到35。","at":-5163,"basis":"jev_attribution","status":"recognized","track":"T4","entry_status":"completed","identity":{"person":"P2","certainty":"确定"}},{"who":"待定声音 1","text":"匠石啊。","at":-24002,"basis":"收到的原话","evidence_ref":"C1"},{"who":"待定声音 2","text":"我现在当网管。","at":-22332,"basis":"收到的原话","evidence_ref":"C2"},{"who":"qf","text":"你才写了20分钟，还不到。","at":-13976,"basis":"收到的原话","evidence_ref":"C3"},{"who":"qf","text":"那这行，我就把这卷写完。","at":-13762,"basis":"收到的原话","evidence_ref":"C4"},{"who":"luguang","text":"到40开始。","at":-10608,"basis":"收到的原话","evidence_ref":"C5"},{"who":"luguang","text":"哎，反正就是到35。","at":-5163,"basis":"收到的原话","evidence_ref":"C6"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793407306344,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"P1","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P1","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P4","source":"scene","support":"无有效证据","names":["待定声音 1"]},{"p":"P5","source":"scene","support":"无有效证据","names":["待定声音 2"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P1"},"mlp":{"p":"P1"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

实际模型返回：

```json
{"items":[{"i":1,"person":"P1","certainty":"confirmed","why":"声纹匹配P1(lux)，个人分类器亦支持P1，联合一致；现场未见冲突"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I1"],"pending_refs":[],"decision_reason":"P1直接以“你”质问匠石是否还玩游戏，指向匠石且语义完整，交回应准备","decision_refs":["I1","C1"]}
```

#### 第二步：回应准备

时间：2026-10-31T08:41:47.436114+08:00；模型：`deepseek-v4-flash`；本阶段耗时：581.6毫秒。

实际 user：

```text
【入口结论】
{"items":[{"i":1,"person":"P1","certainty":"confirmed","why":"声纹匹配P1(lux)，个人分类器亦支持P1，联合一致；现场未见冲突"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I1"],"pending_refs":[],"decision_reason":"P1直接以“你”质问匠石是否还玩游戏，指向匠石且语义完整，交回应准备","decision_refs":["I1","C1"]}

【本次处理原话】
[{"i":1,"at":-347,"text":"你今天还玩游戏是吧？","track":"T5","source":"voice","evidence_ref":"I1"}]

【当前现场与新事件】
{"scene":"【现场概况】约4996ms前有人（P4，声音连续性，身份不确定）招呼“匠石啊”；约3326ms前另一声音（P5，身份不确定）自称“我现在当网管”。两处声纹、MLP均未分档或相互冲突，无法确认对应lux、luguang、qf中的哪位，仅能记录为待定声音。\n\n【话题：招呼与自述】一位待定说话人向匠石打招呼，另一待定说话人告知自己现在当网管；尚不清楚两人是否同一人、是否与匠石继续互动，也无约定或任务提出。匠石是否回应未知。（E1、E2）","events":[{"ref":"E3","p":"P3","text":"你才写了20分钟，还不到。","at":-13976,"basis":"jev_attribution","status":"provisional","track":"T1","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E4","p":"P3","text":"那这行，我就把这卷写完。","at":-13762,"basis":"jev_attribution","status":"provisional","track":"T2","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E5","p":"P2","text":"到40开始。","at":-10608,"basis":"jev_attribution","status":"provisional","track":"T3","entry_status":"completed","identity":{"person":"P2","certainty":"可能"}},{"ref":"E6","p":"P2","text":"哎，反正就是到35。","at":-5163,"basis":"jev_attribution","status":"recognized","track":"T4","entry_status":"completed","identity":{"person":"P2","certainty":"确定"}},{"who":"待定声音 1","text":"匠石啊。","at":-24002,"basis":"收到的原话","evidence_ref":"C1"},{"who":"待定声音 2","text":"我现在当网管。","at":-22332,"basis":"收到的原话","evidence_ref":"C2"},{"who":"qf","text":"你才写了20分钟，还不到。","at":-13976,"basis":"收到的原话","evidence_ref":"C3"},{"who":"qf","text":"那这行，我就把这卷写完。","at":-13762,"basis":"收到的原话","evidence_ref":"C4"},{"who":"luguang","text":"到40开始。","at":-10608,"basis":"收到的原话","evidence_ref":"C5"},{"who":"luguang","text":"哎，反正就是到35。","at":-5163,"basis":"收到的原话","evidence_ref":"C6"}]}

【处理许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

实际模型返回：

```json
{"route":"main","level":1,"recall_memory":true,"main_image":"omit","reply":"","waiting_reply":"","reason":"短聊许可未开，直接质问是否玩游戏需结合过往认识理解，交主认知并保留工具档位"}
```

[请求来源](../../test_runs/entry/online-5b1892cda97a44b9b9da219534eda210/r2/split-overlap-r1/state/step_inputs.jsonl) · [返回及宿主处理](../../test_runs/entry/online-5b1892cda97a44b9b9da219534eda210/r2/split-overlap-r1/events.json)

## 六、程序来源与版本

固定判断正文来自`voice/jev_prompts.py`；合并与分支由`voice/jev_serial.py`组装；user块由`voice/jev_material.py`组装；最终system由`models/prompt.py::build_system`补充当前部署能力和文字规范。原生问题来自`decision/entry_policy.py`。本次更新交往判断及现场修正原则，Schema与user组装不变；未新增模型调用。

实际例子仍是当时请求和返回原文，不替换成模板、不补造成功结果；原生供应商只保留了标准化结果的地方明确标注，不冒充HTTP原始响应。

## 七、历史版本：首轮精简的真实调用对照

记录时间：2026-11-01 15:23（北京时间）。以下来自授权隔离回放，使用当前提示词；system见上文，完整实际system、usage及新旧返回见原始记录。历史例子原文保留。一次通过不代表稳定质量；游戏场景为明确失败例。

[四场景对照报告](../../test_runs/split-upgrade-ab-20261101-152147/对照报告.md)

### 明确告别

评价：明确告别正常进入回应准备，未单独处理短应声。

#### voice_jev_entry

实际user：

```text
【当前输入】
[{"i":1,"p":"unknown","at":-1136,"text":"嗯。","track":"T2","recent_confirmed":{"p":"P1","gap_ms":4170,"same_track":false},"short_voice_hint":"同一短句重复拼接，辅助比较，不是新增证据","source":"voice","evidence_ref":"I1"},{"i":2,"p":"P3","at":-1111,"text":"匠石，我们下去玩了啊。","name":"lux","track":"T3","recent_confirmed":{"p":"P1","gap_ms":4195,"same_track":false},"source":"voice","evidence_ref":"I2"}]

【当前现场·JEV2已编排】
【现场概况】此前一段短对话中，对方提醒匠石喝一口自己杯里的水。仅此一批事件，暂无持续活动、空间或参与关系的新依据。

【话题：提醒喝水】对方以“水喝一口，你杯子里水”提示匠石饮用其杯中的水（E1）。匠石尚未回应或执行；该话适用对象为匠石，属于提醒而非约定。说话者暂归可能为P1（qf）：声纹弱、不可靠，个人分类器与MLP联合指向P1，但分档未定，缺实名确认（E1）。

【尚未编排的新事件】
[{"ref":"E2","p":"P1","text":"你那边儿，沙滩上。","at":-5306,"basis":"jev_attribution","status":"recognized","track":"T1","entry_status":"completed","identity":{"person":"P1","certainty":"确定"}},{"who":"qf","text":"水喝一口，你杯子里水。","at":-10509,"basis":"收到的原话","evidence_ref":"C1"},{"who":"qf","text":"你那边儿，沙滩上。","at":-5306,"basis":"收到的原话","evidence_ref":"C2"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793406894551,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"","strength":"unavailable","basis":"没有本句有效的声纹比较；近期人物和自我介绍另作语义参考"},"candidates":[{"p":"P3","source":"repeated_short_audio","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P2","source":"repeated_short_audio","support":"未分档","names":["luguang"],"sample_limited":false},{"p":"P1","source":"repeated_short_audio","support":"未分档","names":["qf"],"sample_limited":false},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"limited","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]}],"joint":{"state":"conflict","voice_reliable":false,"mlp_high":true,"mlp_priority":true,"voice":{"p":"P3"},"mlp":{"p":"P2"}}},{"ref":"I2","voice":{"pick":"P3","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P3","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P3"},"mlp":{"p":"P3"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

模型返回：

```json
{"items":[{"i":1,"person":"unknown","certainty":"unknown","why":"仅重复短音频“嗯”，声纹不可用，联合分类冲突且优先MLP指向P2，无连续或视觉证据可定人。"},{"i":2,"person":"P3","certainty":"confirmed","why":"声纹清晰匹配P3，个人分类器与声纹一致，语义自报“我们”亦符合lux，无换人反证。"}],"interaction":"direct","utterance_complete":"complete","control":"redirect","route":"prepare","process_refs":["I2"],"pending_refs":[],"decision_reason":"I2直呼匠石并告知“下去玩”，是面向匠石的告别性交往；I1仅为短应声，归属未明，不单独构成交往。","decision_refs":["I2","E1","I1"]}
```

#### voice_jev_prepare

实际user：

```text
【入口结论】
{"items":[{"i":2,"person":"P3","certainty":"confirmed","why":"声纹清晰匹配P3，个人分类器与声纹一致，语义自报“我们”亦符合lux，无换人反证。"}],"interaction":"direct","utterance_complete":"complete","control":"redirect","route":"prepare","process_refs":["I2"],"pending_refs":[],"decision_reason":"I2直呼匠石并告知“下去玩”，是面向匠石的告别性交往；I1仅为短应声，归属未明，不单独构成交往。","decision_refs":["I2","E1","I1"]}

【本次处理原话】
[{"i":2,"at":-1111,"text":"匠石，我们下去玩了啊。","track":"T3","source":"voice","evidence_ref":"I2"}]

【当前现场与新事件】
{"scene":"【现场概况】此前一段短对话中，对方提醒匠石喝一口自己杯里的水。仅此一批事件，暂无持续活动、空间或参与关系的新依据。\n\n【话题：提醒喝水】对方以“水喝一口，你杯子里水”提示匠石饮用其杯中的水（E1）。匠石尚未回应或执行；该话适用对象为匠石，属于提醒而非约定。说话者暂归可能为P1（qf）：声纹弱、不可靠，个人分类器与MLP联合指向P1，但分档未定，缺实名确认（E1）。","events":[{"ref":"E2","p":"P1","text":"你那边儿，沙滩上。","at":-5306,"basis":"jev_attribution","status":"recognized","track":"T1","entry_status":"completed","identity":{"person":"P1","certainty":"确定"}},{"who":"qf","text":"水喝一口，你杯子里水。","at":-10509,"basis":"收到的原话","evidence_ref":"C1"},{"who":"qf","text":"你那边儿，沙滩上。","at":-5306,"basis":"收到的原话","evidence_ref":"C2"}]}

【处理许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

模型返回：

```json
{"route":"main","level":3,"recall_memory":false,"main_image":"omit","reply":"","waiting_reply":"","reason":"I2是面向匠石的告别性交往，内容完整。但短聊许可未开启，不能直接短答，转入主认知简短处理。"}
```

### 家庭游戏问句

评价：家庭对话被错误判为direct并放行，未解决误介入。

#### voice_jev_entry

实际user：

```text
【当前输入】
[{"i":1,"p":"P1","at":-347,"text":"你今天还玩游戏是吧？","name":"lux","track":"T5","recent_confirmed":{"p":"P2","gap_ms":4816,"same_track":false},"source":"voice","evidence_ref":"I1"}]

【当前现场·JEV2已编排】
【现场概况】约4996ms前有人（P4，声音连续性，身份不确定）招呼“匠石啊”；约3326ms前另一声音（P5，身份不确定）自称“我现在当网管”。两处声纹、MLP均未分档或相互冲突，无法确认对应lux、luguang、qf中的哪位，仅能记录为待定声音。

【话题：招呼与自述】一位待定说话人向匠石打招呼，另一待定说话人告知自己现在当网管；尚不清楚两人是否同一人、是否与匠石继续互动，也无约定或任务提出。匠石是否回应未知。（E1、E2）

【尚未编排的新事件】
[{"ref":"E3","p":"P3","text":"你才写了20分钟，还不到。","at":-13976,"basis":"jev_attribution","status":"provisional","track":"T1","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E4","p":"P3","text":"那这行，我就把这卷写完。","at":-13762,"basis":"jev_attribution","status":"provisional","track":"T2","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E5","p":"P2","text":"到40开始。","at":-10608,"basis":"jev_attribution","status":"provisional","track":"T3","entry_status":"completed","identity":{"person":"P2","certainty":"可能"}},{"ref":"E6","p":"P2","text":"哎，反正就是到35。","at":-5163,"basis":"jev_attribution","status":"recognized","track":"T4","entry_status":"completed","identity":{"person":"P2","certainty":"确定"}},{"who":"待定声音 1","text":"匠石啊。","at":-24002,"basis":"收到的原话","evidence_ref":"C1"},{"who":"待定声音 2","text":"我现在当网管。","at":-22332,"basis":"收到的原话","evidence_ref":"C2"},{"who":"qf","text":"你才写了20分钟，还不到。","at":-13976,"basis":"收到的原话","evidence_ref":"C3"},{"who":"qf","text":"那这行，我就把这卷写完。","at":-13762,"basis":"收到的原话","evidence_ref":"C4"},{"who":"luguang","text":"到40开始。","at":-10608,"basis":"收到的原话","evidence_ref":"C5"},{"who":"luguang","text":"哎，反正就是到35。","at":-5163,"basis":"收到的原话","evidence_ref":"C6"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793407306344,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"P1","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P1","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P4","source":"scene","support":"无有效证据","names":["待定声音 1"]},{"p":"P5","source":"scene","support":"无有效证据","names":["待定声音 2"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P1"},"mlp":{"p":"P1"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

模型返回：

```json
{"items":[{"i":1,"person":"P1","certainty":"confirmed","why":"声纹与个人分类器一致指向 P1（lux），联合判定一致且声纹可靠，无换人反证。"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I1"],"pending_refs":[],"decision_reason":"P1 以问句直接面向匠石追问玩游戏，属明确交往；意思完整，交回应准备。","decision_refs":["I1","E6","C6"]}
```

#### voice_jev_prepare

实际user：

```text
【入口结论】
{"items":[{"i":1,"person":"P1","certainty":"confirmed","why":"声纹与个人分类器一致指向 P1（lux），联合判定一致且声纹可靠，无换人反证。"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I1"],"pending_refs":[],"decision_reason":"P1 以问句直接面向匠石追问玩游戏，属明确交往；意思完整，交回应准备。","decision_refs":["I1","E6","C6"]}

【本次处理原话】
[{"i":1,"at":-347,"text":"你今天还玩游戏是吧？","track":"T5","source":"voice","evidence_ref":"I1"}]

【当前现场与新事件】
{"scene":"【现场概况】约4996ms前有人（P4，声音连续性，身份不确定）招呼“匠石啊”；约3326ms前另一声音（P5，身份不确定）自称“我现在当网管”。两处声纹、MLP均未分档或相互冲突，无法确认对应lux、luguang、qf中的哪位，仅能记录为待定声音。\n\n【话题：招呼与自述】一位待定说话人向匠石打招呼，另一待定说话人告知自己现在当网管；尚不清楚两人是否同一人、是否与匠石继续互动，也无约定或任务提出。匠石是否回应未知。（E1、E2）","events":[{"ref":"E3","p":"P3","text":"你才写了20分钟，还不到。","at":-13976,"basis":"jev_attribution","status":"provisional","track":"T1","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E4","p":"P3","text":"那这行，我就把这卷写完。","at":-13762,"basis":"jev_attribution","status":"provisional","track":"T2","entry_status":"completed","identity":{"person":"P3","certainty":"可能"}},{"ref":"E5","p":"P2","text":"到40开始。","at":-10608,"basis":"jev_attribution","status":"provisional","track":"T3","entry_status":"completed","identity":{"person":"P2","certainty":"可能"}},{"ref":"E6","p":"P2","text":"哎，反正就是到35。","at":-5163,"basis":"jev_attribution","status":"recognized","track":"T4","entry_status":"completed","identity":{"person":"P2","certainty":"确定"}},{"who":"待定声音 1","text":"匠石啊。","at":-24002,"basis":"收到的原话","evidence_ref":"C1"},{"who":"待定声音 2","text":"我现在当网管。","at":-22332,"basis":"收到的原话","evidence_ref":"C2"},{"who":"qf","text":"你才写了20分钟，还不到。","at":-13976,"basis":"收到的原话","evidence_ref":"C3"},{"who":"qf","text":"那这行，我就把这卷写完。","at":-13762,"basis":"收到的原话","evidence_ref":"C4"},{"who":"luguang","text":"到40开始。","at":-10608,"basis":"收到的原话","evidence_ref":"C5"},{"who":"luguang","text":"哎，反正就是到35。","at":-5163,"basis":"收到的原话","evidence_ref":"C6"}]}

【处理许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}
```

模型返回：

```json
{"route":"main","level":2,"recall_memory":false,"main_image":"omit","reply":"","waiting_reply":"","reason":"P1以问句追问玩游戏，属需结合语境理解的交往，但短聊许可未开放，故交主认知；当前材料无需召回历史或图片，无工具需求。"}
```


连续场景实测补记：2026-11-01 15:41（北京时间）。三段5–8分钟历史现场中，67个可关联请求点完成新旧对照，11点请求缺失未补造；新版未证明准确性或速度改善，“一点点辣”等误放行仍在，并出现更多输出约束失败。此为固定历史上下文逐轮模型对照，不是新版片场闭环或正式宿主端到端。详见[连续场景对照报告](../../test_runs/long-split-ab-20261101-153412/连续场景对照报告.md)。
