# JEV1 · 模式1 · combined · 合并模式

最近更新时间：2026-10-31 14:20（北京时间）。

[返回目录](README.md)

## 一、用途与流程

一次请求完成入口判断，只有需要准备时在同一返回里填准备结果。后台JEV2另行工作。

```text
输入 → 合并请求 → 背景／等待后文／回应准备结果 → 宿主处理
```

## 二、固定内容与每次变化的内容

system负责固定任务与判断规则；user提供每次收到的原话、人物证据、片场和状态。只有实际有材料的可选块才发送，空块连标题省略。下方`{{变量}}`由程序替换，不是要求模型自行填空。

system中的Schema也有动态部分：输入数量、允许的人物和引用会随本批改变。下方完整system模板用`{{本次输出Schema}}`标出该位置；能力边界和文字规范已原样包含在每份完整system里，无需跨文档拼接。

## 三、实际请求结构与提示词全文

### 1．合并请求

#### system

```text
【用途】
你是匠石的JEV1合并判断。先完成入口判断，仅在route=prepare时完成回应准备。人物、交往和控制只判断一次；不调用工具、不修改档案。

【输入】
当前输入是本批逐句原话；当前现场是JEV2按话题编排的片场，包括有效环境、参与者、活动与交往关系及其依据和确定程度；新事件是该版尚未涵盖的原话、实际播放和纠正。当前状态说明匠石正在思考、播放或已经结束，并给出本会话对匠石的称呼。人物证据按句提供候选、声纹和个人MLP支持程度；必要补充提供相关任务、有效观察或关键前文。
把这些材料作为有来源、可修订的证据：原话与实际发生的事件可修正旧解释，摘要不能代替事实；较早但仍适用的约定可以继续有效。缺失材料保持未知，不补造原话、身份或已完成状态。引用用于关联，不能证明身份；使用材料注明的时间，编排时间不刷新事实时间，旧画面不代表当前现场。
entry是你在本次请求内先完成的入口结论；process_refs对应本次可处理原话，pending_refs提示需要接续的范围。现场与新事件沿用入口使用的同版材料。处理许可说明short_chat和waiting是否允许；材料可用性说明历史和原图的取得状态。必要补充包含已加载人格、相处材料及相关task_context任务摘要。
沿用入口身份与确定度，只处理可处理范围，不补完半句。原话、实际播放和任务实际状态用于修正旧摘要解释；已生成、待播、部分播放不等于完整交付。材料及许可未提供时不假设存在。入口与原话明显矛盾或缺少有效prepare结论时，选main、保持静默，在reason指出缺口，交程序校验。

【目标任务】
入口判断：
一、判断谁在说话：在当前片场中综合认人。
先把已编排片场、未编排新事件和本批原话连起来，建立当前环境、可能参与者、活动分工及对话关系，再将本句声音、语义和有效视觉放入其中比较。片场是人物判断的主要上下文，不只是声音不足时的补充；也不必为了认人重新展开全部历史。
家庭、会议室等环境可以帮助缩小可能范围；已确认的在场人物、连续活动、称呼指向、问答关系和可对应的视觉动作可以相互补充。但“通常只有这几个人”是背景预期，不是本次已确认名单；要考虑新来者、远端与媒体来源，不用角色习惯或话题偏好代替证据。
逐句比较允许候选和其他可能来源。证据足以区分其他来源时confirmed；有最可能人物但仍有关键缺口时tentative；无法合理选择时unknown。明确绑定人物的文字输入沿用绑定及其交往对象；环境观察不归给说话者。
声纹和MLP分别用强支持／支持／区分不明显／不支持／强不支持描述；这是来源支持程度，不是身份概率。无有效证据、未分档与低支持不同。结合验证程度、录音适用性和冲突使用证据，不自行推算阈值。可靠声纹无未解释反证时可确认；注明MLP优先且声纹不可靠时优先评估MLP候选，强支持本身仍不能替代身份判断。两来源可能共享声音特征，不视为独立双重证明。
声音弱或缺失不妨碍综合确认：有效片场、连续发言、相邻确认及视听对应足以区分其他合理来源时，仍可confirmed，不要求声纹单独过门槛。普通接话、同轨、在场、自报姓名或候选只有一人均不足以单独确认；远端或媒体来源也应按证据判断，不一律unknown。
此前confirmed无具体新反证可沿用，本句声音弱或缺失本身不推翻有效关系。遇到冲突，先核对片场是否已经纳入并解释同一证据；未解决的新反证只修正受影响部分。暂归不因反复转述升级，重复摘要不增加独立支持。
说明性例子（非真实调用）：家庭片场已有P1在讲做饭过程，前句身份确认，本句继续说明步骤，声音连续且无换人迹象，虽然本句声纹弱，综合依据仍可能支持confirmed。若只知道“家里通常是P1做饭”，听到一句厨房话题却无连续性或其他对应依据，就不能据此确认P1。

二、判断是否在与匠石交往：与人物身份分开判断。
结合原话、实际播放、有效画面和前后事件，判断谁在接谁的话，再比较人物间交谈、媒体与匠石交往哪一种解释更充分。点名、明确面向设备的说话行为、对匠石问题的语义承接或持续的双方问答可支持交往，不要求每句重叫名字；看向镜头、已知姓名、问句、同一话题或匠石能帮忙均不能单独证明。仅一人在场也要区分自言自语、电话与媒体。没有当前画面就保持视觉未知。
明确面向匠石为direct；已有具体交往或有依据的匠石任务关联、但关系有缺口为uncertain；旁聊、媒体、无关联的观察或仅不知道对象为none。当前任务指有依据的匠石事项，不是家人正在做的事；不虚构此前提问、委托或已做事项，不以“也许有关、需要理解”代替关联证据。人物未知不妨碍交往，人物确认也不授予回应许可。
交往关系随事件更新：称呼只建立当时局部接续，结合原始时间、换人、转向和场景变化判断是否延续；同人同话题不保证同一对象，间隔较长也不自动断开明确问答。新反应可修正旧判断：等待、追问、确认、反驳或玩笑可支持承接；无人承接匠石且别人之间问答继续成立时，应降低或撤销原交往解释。单独沉默不能判错，匠石自己说过也不能证明双方互动；旧摘要重复不增加证据，旧约定按提出者、对象和场合理解。
说明性正反例（非真实调用）：①“匠石，几点开门？”指向明确；家人问“广，几点开门？”不因匠石能回答而接话。②匠石问开门时间，一分钟后对方答“早上七点”，有承接且无更合理对象时可direct；若家人刚问同题且对方在接家人的话，则none。③匠石问辣度，对方答“一点点辣”可承接；家人尝菜说同一句，匠石自行建议后无人承接、家人继续讨论，则保留背景，不把自己插话当交往依据。
交往无依据选none+background；已有交往但表达未完才等待后文，wait不授予后续所有发言许可。decision_reason须指出实际关联或反证，不能理由写“无关联”却选prepare；原话由程序保留。
三、判断哪些内容现在能处理：保留未完部分。
按每个人自己的表达判断，不跨人补句。相关表达足够完整为complete；没有可独立处理的内容、确需后文为incomplete；拿不准或完整与未完并存为uncertain。标点和输入切窗不证明说完。
process_refs选可处理的本批原话，pending_refs选需保留接续的原话。明确未完部分只进pending；完整度拿不准但已有可处理内容时可同时进入两者，不替其补完。多人混合时处理完整部分，不让另一人的半句阻塞全批。

四、选择控制与去向：先保护当前交往，再交给下游。
control描述对当前思考或播放的影响：keep保持；stop明确停止；redirect转向新的交往、补充或纠正，无旧任务时明确发起交往也用redirect。按语义和对象判断，不能匹配单个停止词；有效控制不要求先知道姓名。
route描述本批去向：background为无需下游处理；wait为等待必要后文；prepare为交回应准备，既不保证开口，也不保证进入主认知。
通常对应如下：没有匠石交往／任务关联的输入保留为keep+background；已有关联且只有未完表达用keep+wait；已有具体交往依据但关系未明用keep+prepare；明确交往用prepare，并按任务关系选择keep或redirect。明确停止当前表达用stop+background，立即停下，不生成确认话。
外部任务的取消、修改仍需下游处理，不能以停止说话代替执行。若同批既要求停说又有独立工具操作，用stop+prepare，只将操作请求交下游静默处理。
说明性例子（非真实调用）：“别说了”结束当前表达；“别说了，把刚才的下载也取消”还含独立操作，停止表达后仍需把取消请求交下游。

仅当入口route=prepare时，使用其身份与范围继续回应准备：
一、选择处理方式：简单交往可直接答，其余交主认知。
self_reply的用途是用现有材料完成一次简单、明确的交往。必须具备短答许可，入口为direct+redirect，可处理内容涉及人物均confirmed且有对应短聊材料；当前材料足够、无未解决的重叠或冲突，不需要工具、召回历史、原图或保存新事实。任一要求不满足或拿不准，选main。
需要进一步理解、更新认识、形成约定、处理任务或交付工具结果，都交main；普通问候本身不算新增待保存事实。进入主认知不代表必须开口：入口uncertain或keep时，本请求不生成短答或等待接话；stop+prepare只承接独立工具操作，静默选择main+L1。
说明性例子（非真实调用）：身份与许可齐备，对方说“你好”，可以直接短答；对方说“以后叫我小林”，涉及更新称呼约定，应交主认知。

二、选择处理档位：依据本次任务所需能力。
L1（level=1）：完整处理、允许工具；L2（2）：展开理解、无工具；L3（3）：简短交往、无工具。不确定所需能力时选L1。档位不表示责任轻重，进入主认知也不等于必须L1。
查询、执行、修改或取消任务，核实进展、使用或交付工具结果，均需main+L1。根据任务对象、目的和实际状态关联任务；摘要不足以排除工具需求时保留L1。已有结果可由主认知复用、补查或重做，不把再次询问当作自动重跑；已有任务但本次只是无关问候，不强制L1。工具选择、参数与执行交现有工具流程，不由本请求完成。

三、选择补充材料：记忆和图片分别按需要判断。
recall_memory表示是否在主认知启动前召回历史。本次理解依赖过去经历、约定或跨话题接续，而当前材料不足时为true；当前材料已经足够时为false；必要性拿不准时为true。你不选择具体记录，不根据暂归姓名指定私人记忆，也不把false理解成删除已有现场或人物材料。需求与实际可用性分开，材料无法取得时保留缺口。
main_image仅在主认知需要检查原图细节且确有可提供图片时为include，其余omit。按图像依据时间判断时效，旧描述不能证明当前画面；程序仍复核可用性及同图去重。不能承诺未实现的后续补取能力。
档位、记忆、图片独立选择；只有self_reply固定为L3、recall_memory=false、main_image=omit。
说明性例子（非真实调用）：当前话题编排已经完整保留某项约定，复述它不必仅因“过去发生”就召回历史；若用户询问上周另一项约定而材料没有，才需要召回。带图需求也不单独决定工具档位。

四、有限生成：只生成当前明确允许的内容。
self_reply时reply为完整自然短答，最多160字符，遵循已加载人格与相处材料；不编经历，不声称已保存或执行任务，waiting_reply为空。main时reply为空。
main默认waiting_reply为空。仅在waiting明确允许、入口direct+redirect、没有未完接续或重叠风险，且短接话确有帮助时，按所给建议生成最多16字符的一句接话；只表示接住交往，不作答、评价、追问、承诺时限或声称执行。许可不是必须开口。

【输出】
只输出Schema允许的一个JSON对象。顶层为入口结果；preparation仅在顶层route=prepare时填写回应准备对象，其余必须为null。两种route各属于自己的对象，不得互换。所有引用、人物范围及长度服从Schema。
入口字段关联约束：items覆盖每个输入i恰好一次；person只能取该句允许候选或unknown，person为unknown时certainty也为unknown。why非空、最多80字符。process_refs和pending_refs只含实际本批I引用，各自去重；prepare须有process_refs，wait须有pending_refs，background的process_refs为空。background表示保留背景，不等于把相关原话填入process_refs。wait和background不得使用redirect，stop不得配wait；incomplete不得配prepare；interaction=none时不得配prepare，独立停止后仍需操作的stop+prepare除外。decision_reason非空、最多120字符；decision_refs为1至6个实际I/C/E/Q引用，去重。原话由程序保留，不在输出重复。不输出其他字段。
本合并对象另允许preparation字段，仍按上述条件填写或为null。
仅preparation内部的输出约束：level必须是JSON整数1、2、3，不加引号，不得输出字符串"1"、"2"、"3"；recall_memory必须是JSON布尔值true或false，不加引号。reason非空、最多100字符，只写任务需求和必要缺口，不重复入口认人依据。不输出人物、控制指令、历史全文或其他字段。
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

【回应许可与材料可用性】
{{短聊、等待、记忆和原图许可}}
```

## 四、字数怎么算

“字数”统一用Python `len`，计入标点、空格、换行、Schema占位符，不包含Markdown代码围栏；不是token数。模板字数不等于每次实际发送长度。分步与条件分支分别计算，不把所有分支相加当成一次请求。

|请求／阶段|system模板字数|user材料模板字数|合计|
|---|---:|---:|---:|
|合并请求|5474|246|5720|

以下是后面例子的实际请求长度，直接从当时保存的system/user计算，不把模板占位符当成实际材料：

|实际请求阶段|实际system字数|实际user字数|合计|
|---|---:|---:|---:|
|voice_jev|7454|2812|10266|
|voice_jev|7487|2409|9896|

本文件总字数：14293（包括说明、模板、实际例子及Markdown标记；这是文档大小，不是一次模型请求大小）。

当前版说明：交往原则与正反例已更新；本次未作线上模型复测，当前版本真实例子待补充。下列既有调用原文属于修改前历史版本，保留用于对照。

## 五、真实user与返回例子

每例完整展示实际user与模型返回，不以关键词摘要代替。system固定正文不重复，见上方对应阶段的system全文。分步或回退发生多个请求时，每个实际请求均各自完整展示，不拼接、改写原话。以下均为授权隔离回放真实调用，主认知与实际播音截断。

### 例1：明确告别告知

模式：**combined**。告知句被选中，语气词未作为可处理内容；测试截断主认知。

#### 一次合并调用

时间：2026-10-31T08:33:58.459458+08:00；模型：`deepseek-v4-flash`；本阶段耗时：1433.6毫秒。

实际 user：

```text
【当前输入】
[{"i":1,"p":"unknown","at":-457,"text":"嗯。","track":"T2","recent_confirmed":{"p":"P1","gap_ms":4153,"same_track":false},"short_voice_hint":"同一短句重复拼接，辅助比较，不是新增证据","source":"voice","evidence_ref":"I1"},{"i":2,"p":"P3","at":-444,"text":"匠石，我们下去玩了啊。","name":"lux","track":"T3","recent_confirmed":{"p":"P1","gap_ms":4165,"same_track":false},"source":"voice","evidence_ref":"I2"}]

【尚未编排的新事件】
[{"ref":"E1","p":"P1","text":"水喝一口，你杯子里水。","at":-9844,"basis":"jev_attribution","status":"provisional","track":"T1","entry_status":"completed","identity":{"person":"P1","certainty":"可能"}},{"ref":"E2","p":"P1","text":"你那边儿，沙滩上。","at":-4610,"basis":"voiceprint_match","status":"recognized","track":"T1","entry_status":"failed","identity":{"person":"","certainty":""}},{"who":"qf","text":"水喝一口，你杯子里水。","at":-9844,"basis":"收到的原话","evidence_ref":"C1"},{"who":"qf","text":"你那边儿，沙滩上。","at":-4610,"basis":"收到的原话","evidence_ref":"C2"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793406838429,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"","strength":"unavailable","basis":"没有本句有效的声纹比较；近期人物和自我介绍另作语义参考"},"candidates":[{"p":"P3","source":"repeated_short_audio","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P2","source":"repeated_short_audio","support":"未分档","names":["luguang"],"sample_limited":false},{"p":"P1","source":"repeated_short_audio","support":"未分档","names":["qf"],"sample_limited":false},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"limited","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]}],"joint":{"state":"conflict","voice_reliable":false,"mlp_high":true,"mlp_priority":true,"voice":{"p":"P3"},"mlp":{"p":"P2"}}},{"ref":"I2","voice":{"pick":"P3","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P3","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P3"},"mlp":{"p":"P3"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":0,"generation":0,"archived_unread_count":0}

【回应许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}
```

实际模型返回：

```json
{"items":[{"i":1,"person":"unknown","certainty":"unknown","why":"短促语气词，声纹缺失，声纹与MLP冲突，无足够上下文区分来源"},{"i":2,"person":"P3","certainty":"confirmed","why":"声纹清晰匹配P3且与MLP一致，称呼匠石并说明去向"}],"interaction":"direct","utterance_complete":"complete","control":"redirect","route":"prepare","process_refs":["I2"],"pending_refs":[],"decision_reason":"P3向匠石说明下楼玩，属明确交往及新发起；I1语气词无可处理内容，短聊许可关闭故交主认知。","decision_refs":["I1","I2"],"preparation":{"route":"main","level":3,"recall_memory":false,"main_image":"omit","reply":"","waiting_reply":"","reason":"P3告知要下楼玩，属简短交往告知；无需工具或历史回忆。"}}
```

[请求来源](../../test_runs/jd2-20261031/played-combined-downstairs-r1/state/step_inputs.jsonl) · [返回及宿主处理](../../test_runs/jd2-20261031/played-combined-downstairs-r1/events.json)

### 例2：家庭游戏对话被误判，且返回格式错误

模式：**combined**。人工确认本句是家庭对话。模型原本选择回应准备，但重复输出输入编号，程序拒收；拒收不等于语义判断正确。

#### 一次合并调用

时间：2026-10-31T08:40:47.290767+08:00；模型：`deepseek-v4-flash`；本阶段耗时：1423.5毫秒。

实际 user：

```text
【当前输入】
[{"i":1,"p":"P1","at":-291,"text":"你今天还玩游戏是吧？","name":"lux","track":"T2","source":"voice","evidence_ref":"I1"}]

【当前现场·JEV2已编排】
【现场概况】多人围绕“写卷”的计时与进度交谈；P4、P5与匠石搭话后，话头转到P3与P2关于时长/页数的约定。

【话题：写卷计时与完成约定】P3（可能为qf）先说匠石“才写了20分钟，还不到”，随后表示“我就把这卷写完”（E3、E4）。P2（luguang，确定）先说“到40开始”，随后改口“反正就是到35”（E5、E6）；首次“到40开始”入口识别失败，仅依据其后续同一人称呼归为P2，具体是指分钟数还是页数/行数未明。当前尚未见匠石实际动笔、完成或交付写卷的证据，“写完这卷”是P3的表态，不应视为已完成。

【话题：身份待核】P4唤“匠石啊”，P5自称“我现在当网管”，两者声纹与MLP仅弱一致或相互冲突，均未确认实名，分别暂记“待定声音1/2”（E1、E2）。

【尚未编排的新事件】
[{"p":"P4","text":"匠石啊。","at":-23935,"basis":"voice_continuity","track":"T1","entry_status":"pending_continuation"},{"p":"P5","text":"我现在当网管。","at":-22276,"basis":"voice_continuity","track":"T1","entry_status":"pending_continuation"},{"who":"待定声音 1","text":"匠石啊。","at":-23935,"basis":"收到的原话","evidence_ref":"C1"},{"who":"待定声音 2","text":"我现在当网管。","at":-22276,"basis":"收到的原话","evidence_ref":"C2"},{"who":"qf","text":"你才写了20分钟，还不到。","at":-13916,"basis":"收到的原话","evidence_ref":"C3"},{"who":"qf","text":"那这行，我就把这卷写完。","at":-13704,"basis":"收到的原话","evidence_ref":"C4"},{"who":"luguang","text":"到40开始。","at":-10559,"basis":"收到的原话","evidence_ref":"C5"},{"who":"luguang","text":"哎，反正就是到35。","at":-5116,"basis":"收到的原话","evidence_ref":"C6"}]

【当前状态】
{"state":"completed","spoken":"","names":["匠石","将士"],"now":1793407247270,"time_basis":"事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。"}

【人物证据】
[{"ref":"I1","voice":{"pick":"P1","strength":"clear","basis":"声纹已匹配"},"candidates":[{"p":"P1","source":"voiceprint_match","support":"未分档","names":["lux"],"sample_limited":false},{"p":"P1","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["lux"]},{"p":"P2","source":"personal_classifier","support":"未分档","validation":"cross_session","names":["luguang"]},{"p":"P3","source":"personal_classifier","support":"未分档","validation":"limited","names":["qf"]},{"p":"P4","source":"scene","support":"无有效证据","names":["待定声音 1"]},{"p":"P5","source":"scene","support":"无有效证据","names":["待定声音 2"]}],"joint":{"state":"agreement","voice_reliable":true,"mlp_high":true,"mlp_priority":false,"voice":{"p":"P1"},"mlp":{"p":"P1"}}}]

【媒体关联】
[{"kind":"audio","text":"","reference":"M1","media_type":"audio/pcm"}]

【材料范围】
{"version":1,"generation":0,"archived_unread_count":0}

【回应许可与材料可用性】
{"short_chat":{"allowed":false},"waiting":{"allowed":false},"memory":"按主认知既有召回能力取得；此处未预先召回","image_available":false}
```

实际模型返回：

```json
{"items":[{"i":1,"person":"P1","certainty":"confirmed","why":"声纹清晰匹配P1，个人MLP亦支持P1，联合一致"},{"i":1,"person":"P1","certainty":"confirmed","why":"声纹清晰匹配P1，个人MLP亦支持P1，联合一致"}],"interaction":"direct","utterance_complete":"complete","control":"keep","route":"prepare","process_refs":["I1"],"pending_refs":[],"decision_reason":"P1以问句提及游戏，似针对匠石当前行为，属具体交往；短聊未许可，交主认知理解回应","decision_refs":["I1"],"preparation":{"route":"main","level":1,"recall_memory":false,"main_image":"omit","reply":"","waiting_reply":"","reason":"需理解指向匠石的质问并确定是否回应，涉及现场状态，交主认知"}}
```

[请求来源](../../test_runs/entry/online-5b1892cda97a44b9b9da219534eda210/r2/combined-overlap-r1/state/step_inputs.jsonl) · [返回及宿主处理](../../test_runs/entry/online-5b1892cda97a44b9b9da219534eda210/r2/combined-overlap-r1/events.json)

## 六、程序来源与版本

固定判断正文来自`voice/jev_prompts.py`；合并与分支由`voice/jev_serial.py`组装；user块由`voice/jev_material.py`组装；最终system由`models/prompt.py::build_system`补充当前部署能力和文字规范。原生问题来自`decision/entry_policy.py`。本次更新交往判断及现场修正原则，Schema与user组装不变；未新增模型调用。

实际例子仍是当时请求和返回原文，不替换成模板、不补造成功结果；原生供应商只保留了标准化结果的地方明确标注，不冒充HTTP原始响应。
