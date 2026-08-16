# Paper Agent 功能扩展设计：从文献调研到投稿全流程

> 本文档是当前扩展方案，只保留最新计划，不写历史沿革；已落地架构以 [docs/DESIGN.md](docs/DESIGN.md) 为准。
> 设计立场：在现有"对话唯一入口 + 原子工具/技能双层"框架上生长，新能力全部与既有机制耦合，不另起炉灶。

> **实施状态（2026-08-07 第一阶段）**
> - 已落地 ✅（5 个）：`integrity_sweep` / `evidence_anchor` / `bib_import` / `exhibit_index` / `field_census`。详见各节 ✅ 标记与 DESIGN §5.4 / §5.5 / §6.3 / §9.4。
> - 待设计 ⏳（4 个，本计划 §8/§9 引用为"投稿主线"终点，但 §3-7 未给出契约/frontmatter/流程，需先补设计）：`venue_radar` / `rebuttal_planner` / `cover_pitch` / `proposal_architect`。
> - 后续阶段（未动工）：`frontier_pulse` / `study_notes` / `term_compass` / `scholar_translate` / `title_tuner` / `draft_delta`。其中 `frontier_pulse` 需给 `SearchBackend.search` 基类加 `date_from` 参数并穿透 9 个 backend，是最侵入的改动。

## 0. 设计原则（沿用现有红线，新能力必须遵守）

- 对话是唯一交互入口；能力分两层：确定性/数据通道型做成 typed 工具（pydantic schema 单一事实源），工作流型做成 `skills/builtin/<name>/SKILL.md` 指令技能（零代码注册、渐进式披露）。
- **事实由代码算，建议由模型给**：凡是 API 可查、规则可判的（撤稿状态、venue 直方图、图表 caption），一律确定性代码，LLM 只做归纳与解读。
- 严控 token：每个新能力标注 LLM 调用预算上限；归纳类调用走 `ainvoke_utility`（关 thinking）；prompt 全部进注册表带版本。
- 会话级隔离、跨会话零记忆；新产物随会话 JSON 持久化或当轮输出，不做跨会话文献库。
- 数据合规：OA-only；新增 API 调用一律走 `tools/search/base.py::RateLimiter` 进程级限流；邮箱策略不变（无配置则匿名/不调用）。
- 命名规范：snake_case；与既有 12 工具、12 技能不撞名、不近义混淆；不复用外部产品的功能命名。

## 1. 现状盘点与缺口分析（按研究生命周期）

| 生命周期阶段 | 已有能力 | 缺口 |
|---|---|---|
| 选题/立项 | topic_advisor、research_gap、structure_advisor | 开题报告/研究计划书成文 |
| 文献检索 | search_papers（9 源+重排分层） | 一次性快照，无持续性新文追踪 |
| 文献获取 | citation_export（仅导出） | 用户已有 .bib 文献库无法导入会话 |
| 深读理解 | deep_read、ask_papers、compare_papers、paper_critique | 图表导览（先看图读故事线）、术语对齐表、面向"为我所用"的阅读笔记 |
| 证据核对 | draft_review（相关工作漏引对照） | 任意草稿段落的逐句"该引哪篇"证据锚定 |
| 可靠性 | 无 | 撤稿/勘误/预印本→正式版检查（综述与投稿前的刚需） |
| 地图/脉络 | research_map、reading_path、谱系图 | 全库宏观计量（年度趋势/作者/机构/期刊画像） |
| 综述写作 | write_review、related_work | （覆盖良好） |
| 论文写作 | paper_writing 四模式 | 学术翻译（中英互译、术语一致） |
| 草稿检查 | check_structure、check_format、format_compliance、draft_review | （覆盖良好） |
| 汇报 | presentation_prep（组会） | （覆盖良好） |
| 导出 | export_manuscript、export_report | （覆盖良好） |

## 2. 新能力总览

| 模块 | 名称 | 类型 | 前置门控 | LLM 预算 | 优先级 |
|---|---|---|---|---|---|
| 一·动态情报 | `frontier_pulse` | 原子工具 | papers | ≤1（utility） | P0 |
| 二·阅读增强 | `exhibit_index` ✅ | 可执行技能（确定性） | 全文 | 0 | P1 |
| 二·阅读增强 | `study_notes` | 指令技能 | papers | 复用 RAG | P1 |
| 二·阅读增强 | `term_compass` | 指令技能 | papers | 1+复用 RAG | P1 |
| 三·证据与引用 | `evidence_anchor` ✅ | 指令技能 | papers | 1+≤8 检索+1 | P0 |
| 三·证据与引用 | `integrity_sweep` ✅ | 原子工具（确定性 API） | papers | 0 | P0 |
| 三·证据与引用 | `bib_import` ✅ | 原子工具 | attachments(.bib) | 0 | P1 |
| 四·领域计量 | `field_census` ✅ | 原子工具 | papers | 1（utility） | P1 |
| 五·语言与成稿 | `scholar_translate` | 指令技能 | 无 | 按篇幅 | P1 |
| 五·语言与成稿 | `title_tuner`（可选） | 指令技能 | attachments | 1-2 | P2 |
| 五·语言与成稿 | `draft_delta`（可选） | 原子工具（确定性） | attachments×2 | 0 | P2 |

命名冲突核对：以上 15 个名字与既有工具/技能（search_papers、deep_read、ask_papers、research_map、reading_path、write_review、check_structure、check_format、export_manuscript、use_skill、citation_export、export_report、compare_papers、research_gap、paper_critique、presentation_prep、draft_review、related_work、paper_writing、topic_advisor、structure_advisor、format_compliance）无一相同、无近义混淆；均为本项目原创命名。

## 3. 模块一 · 动态情报

### 3.1 `frontier_pulse`（前沿脉动）— 新原子工具，P0

**定位**：search_papers 是"一次性快照"，研究者需要持续跟踪。本工具基于会话既有检索式做日期窗增量检索，产出"新增文献简报"并自动并入候选集。

**输入契约**（pydantic）：
- `days: int = 30`（1-90，时间窗天数）
- `focus: str = ""`（可选，限定某个子方向）

**前置守卫**：会话无 `search_queries`/核心集 → `NO_PAPERS`，消息内写明"请先 search_papers"。

**内部流程（文字步骤）**：
1. 从 `session.search_queries` 取既有检索式（focus 非空时只用匹配子方向的检索式）；无检索式回退 `session.topic`。
2. `SearchManager.search_all` 并发检索，各源 backend 增加日期过滤参数：OpenAlex 用 `from_publication_date` 过滤器、arXiv 用 `submittedDate` 区间语法（feedparser 解析不变）、Crossref 用 `from-pub-date`、Europe PMC 用 `FIRST_PDATE`；不支持日期过滤的源（DOAJ 等）跳过并在结果中注明。全部走既有限流器与 120s 全局 deadline。
3. 与会话已有论文（核心集∪候选集）去重：DOI 精确 + 标题归一化（复用 `tools/search` 去重函数），得到纯增量集合。
4. 增量论文按既有重排公式打分排序；若 `session.map_data` 存在，用嵌入余弦归属到最近主题簇（嵌入不可用时按子方向分组）。
5. 1 次 utility LLM 归纳简报：每篇一句话"新在哪、与会话哪篇核心论文构成什么关系（新证据/新挑战/综述性进展）"；失败回退纯列表。
6. 增量论文自动并入 `session.candidates` 并写会话向量索引 L1（best-effort），结果注明"已并入候选集"。

**输出**：`{new_papers, window_days, per_cluster, brief}`；前端新卡片"前沿简报"（按簇分组列表 + 关系标注）。

**降级链**：日期过滤不可用的源→跳过并明示；嵌入不可用→按子方向分组；LLM 失败→纯结构化列表；增量为空→明确回复"窗口内无新增"（不触发规则反射器，属正常态）。

**耦合点**：`agents/chat_tools.py`（schema）、`agents/tools_impl.py`（`_IMPLS`）、`tools/search/` 各 backend（日期参数）、`tools/storage/vectorstore`（L1 索引）、前端 `ChatMessage.tsx`（卡片）、`tests/eval/`（检索 golden 可选扩充）。

**测试**：日期参数构造纯函数单测（各源语法）、增量去重单测、stubbed SearchManager 的端到端流转。

## 4. 模块二 · 阅读增强

### 4.1 `exhibit_index`（图表导览）— 可执行技能/确定性工具，P1

**定位**：先读图表 caption 再决定精读哪里，是高效的论文读法。纯文本模型看不懂图，但 caption 是文本——提取 caption 序列完全合法且零成本，符合"事实由代码算"。

**输入契约**：`paper_ids: list[str] = []`（空 = 会话内所有已有全文的论文）。

**内部流程**：对每篇已下载全文（`data/pdfs/` 或会话全文缓存）用 PyMuPDF 逐页扫描，正则匹配 `^(Figure|Fig\.|TABLE|Table|图|表)\s*\d+` 起始的 caption 段落并合并同页续行；按文档顺序输出每篇的图表清单：编号、类型（图/表）、caption 全文、页码、所属章节（若可判）。零 LLM。无全文的篇目列入"未获取全文"清单，并提示可通过 deep_read(focus) 升级或上传 PDF 后重试。

**输出**：`{exhibits: {paper_id: [...]}, missing_fulltext: [...]}`；前端"图表导览"卡片（按论文分组的 caption 列表，点击 caption 可复制进聊天框追问）。

**联动**：presentation_prep（汇报时讲图）、compare_papers（实验设置对比）可在指令中引用本工具结果。

**耦合点**：`tools/pdf/` 新增 caption 提取模块；`agents/chat_tools.py` + `tools_impl.py`；前端卡片。

**测试**：caption 正则与续行合并（构造多版式文本 fixture）、空全文守卫。

### 4.2 `study_notes`（研习笔记）— 指令技能，P1

**定位**：deep_read 提取的是"论文里有什么"，本技能产出的是"对我有什么用"——结构化研习笔记卡，面向复用与写作素材沉淀。

**frontmatter 草案**：
- name: study_notes；version: 1；requires: [papers]
- description 草案：「生成结构化论文研习笔记（研究问题/方法骨架/关键数据/局限/可借鉴点/与我课题的关系/待深究问题）。当用户说"帮我做论文笔记""整理阅读笔记""把这篇论文的要点记下来""做个读书报告"时使用。与 deep_read 的区别：deep_read 是结构化事实提取，本技能是面向复用的笔记再加工。」
- output_check 草案：「每篇笔记七个字段齐全；所有事实性内容来自会话论文并带 [paper_id]，未报告的写"未报告"不编造；"与我课题的关系"基于用户明示的课题写，课题未知就先问。」

**工作流**：确定篇目（默认核心集或用户指定）→ 每篇经 ask_papers/deep_read 取证（缓存命中零成本）→ 按七字段模板生成笔记 → 提示可 `export_manuscript` 导出 md 存档。

### 4.3 `term_compass`（术语罗盘）— 指令技能，P1

**定位**：一个领域的术语中英对照 + 权威定义，是读文献和写论文（尤其英文写作）的基础设施；也为 scholar_translate 提供术语一致性来源。

**frontmatter 草案**：
- name: term_compass；version: 1；requires: [papers]
- description 草案：「从会话论文构建领域术语表：英文术语、中文译名、一句话定义、定义出处、同义/近义辨析。当用户说"这个领域的术语帮我整理一下""这些概念都是什么意思""做个术语表/名词解释""X 和 Y 有什么区别"时使用。」
- output_check 草案：「术语全部出自会话论文（定义带 [paper_id] 出处）；译名遵循论文中已有的中文惯例，无出处依据的译名标"建议译法"；不编造定义。」

**工作流**：ask_papers("核心概念与术语的定义与区分", top_k 8-10) 取证 → 1 次 LLM 归并成表 → 写入会话供后续技能引用（scholar_translate、paper_writing 润色时强制对齐术语表）→ 可导出 md。

## 5. 模块三 · 证据与引用

### 5.1 `evidence_anchor`（证据锚定）— 指令技能，P0

**定位**：写作中最易被审稿人攻击的是"无据主张"。用户贴出任意草稿段落，系统逐句找出可证伪主张，并从会话文献中为每处主张锚定该引用的论文与证据强度。与 draft_review 明确分工：draft_review 是整篇评审 + 相关工作漏引对照；evidence_anchor 是任意段落的逐句证据核对，dispatch 描述写清边界。

**frontmatter 草案**：
- name: evidence_anchor；version: 1；requires: [papers]
- description 草案：「逐句核对草稿段落的证据支撑：拆出可证伪主张，为每条主张从会话文献中找支持/相悖证据并建议引用位置。当用户贴出一段话说"这段话该引哪些文献""这些论断有文献支持吗""帮我检查这段的证据"时使用。整篇草稿的评审用 draft_review；本技能面向局部段落的证据核对。」
- output_check 草案：「每条主张都有结论（直接支持/间接支持/无证据/有相悖证据）；推荐引用全部来自会话论文集并带 [paper_id]；无证据的主张明确标"需自补实验或另行外引"，不硬凑。」

**工作流**：1 次 utility LLM 把段落拆成 ≤8 条可证伪主张 → 每条经 ask_papers(top_k=5) 取证 → 1 次 LLM 汇总成"原文句 → 主张 → 证据与强度 → 建议插入引用位置"对照表 → 收尾列出无证据主张清单。

**预算**：1 + ≤8 次检索 + 1 次汇总。

### 5.2 `integrity_sweep`（可靠性质检）— 新原子工具（确定性 API 调用），P0

**定位**：引用被撤稿的论文是综述与投稿的硬伤；arXiv 预印本已有正式版时应引正式版。这些状态全部可由官方 API 确定性查询，零 LLM，是"写综述前/投稿前必跑"的质检闸。

**输入契约**：`paper_ids: list[str] = []`（空 = 核心集∪候选集全部）。

**内部流程**：逐篇（asyncio 并发 + 既有限流器）：
1. 有 DOI：查 OpenAlex `works/doi:{doi}` 读 `is_retracted` 字段；查 Crossref `works/{doi}` 读 `relation`（is-retraction-of / has-correction / has-expression-of-concern）与 `update-to` 链。
2. 有 arXiv ID：读 arXiv 条目的 `journal_ref` / 关联 DOI——存在即提示"已有正式发表版本，建议改引正式版"。
3. 汇总健康报告：每篇标记 ✅ 无异常 / ⚠️ 有勘误或关切声明 / ⛔ 已撤稿（并给出撤稿通知来源）/ 🔁 预印本已有正式版；查询失败的篇目标"未查到，请人工核对"（诚实降级，不算错误）。

**输出**：`{report, facts}`；前端"可靠性质检"卡片（分级色标的清单）。

**自动建议**：`_SKILL_HINTS` 与系统 prompt L2 增加规则——write_review 完成后、export_manuscript 投稿稿前，提示运行本工具。

**耦合点**：`tools/search/openalex.py`、`tools/search/crossref.py` 各增单条查询函数（复用限流）；`agents/chat_tools.py` + `tools_impl.py`；前端卡片。

**测试**：OpenAlex/Crossref 响应解析纯函数（fixture JSON）、报告聚合分级逻辑、查询失败降级。

### 5.3 `bib_import`（文献库导入）— 新原子工具，P1

**定位**：用户已有的 Zotero/EndNote 导出 .bib 是现成的文献清单；导入后即可对这批论文用全套能力（深读/地图/综述），与 citation_export 构成双向互通。

**输入契约**：`attachment: str = ""`（已上传 .bib 附件的 id 或文件名片段；空 = 最近上传的 .bib）。

**内部流程**：
1. 上传通道放行 `.bib`（本质是文本，走 `tools/ingest/downloader` 现有文本路径，无需新下载器）。
2. 纯 Python 容错 BibTeX 解析器（`@type{key, field = {...}}` 最小语法 + 嵌套花括号/引号值；不引新依赖）：逐条提取 title/author/year/doi/venue/abstract。
3. 有 DOI 的条目经 OpenAlex/Crossref 补全元数据（复用缓存与限流）；无 DOI 的用标题模糊查 OpenAlex（Jaccard≥0.95 才确认，否则保留原样并标注）。
4. 构造 Paper 对象并入 `session.candidates`（不污染核心集），写会话向量索引 L1。
5. 输出导入报告：成功 N 条、补全 M 条、解析失败 K 条（逐条列出原因）。

**LLM**：0。**测试**：解析器纯函数（多风格 .bib fixture）、补全与匹配 stubbed、候选集并入与去重。

## 6. 模块四 · 领域计量

### 6.1 `field_census`（领域普查）— 新原子工具，P1

**定位**：research_map 回答"我手里这批论文的语义结构是什么"，field_census 回答"整个领域在宏观上长什么样"——年度趋势、谁在主导、发在哪。全部走 OpenAlex group_by 聚合，确定性统计。

**输入契约**：无参数（用会话检索式）；前置守卫同 research_map。

**内部流程**：以会话检索式对应的 OpenAlex search filter 调聚合接口：按 `publication_year` 分组取近 15 年趋势、按作者分组取 top10、按机构分组取 top10、按来源（期刊/会议）分组取 top10；再对 top 来源查 sources 接口取画像（发文量、被引、是否 OA、主题分布）。确定性聚合后，1 次 utility LLM 写三句话画像（领域在增长还是饱和、谁主导、主要发在哪），失败回退纯表格。

**输出**：`{yearly, top_authors, top_institutions, top_venues, portrait}`；前端"领域普查"卡片（纯 SVG 趋势折线 + 三个 top 榜，沿用 GenealogyGraph 零依赖原则）。

**边界**：dispatch 描述写清——"画领域统计/趋势/谁高产"用本工具；"整理论文间关系结构"用 research_map。

**测试**：聚合响应解析纯函数、画像文本 stubbed。

## 7. 模块七 · 语言与成稿

### 7.1 `scholar_translate`（学术译写）— 指令技能，P1

**定位**：paper_writing 模式 D 是单语言润色，本技能是跨语言学术翻译——术语一致性（联动 term_compass 产物）、引用标记与公式零改动，是中文研究者写英文论文的高频刚需。

**frontmatter 草案**：
- name: scholar_translate；version: 1；requires: []
- description 草案：「学术论文中英互译：术语全文一致（优先对齐会话术语表）、保留引用编号/公式/变量、学术语体。当用户说"把这段翻译成英文/中文""帮我翻译摘要""这段中译英要学术一点"时使用。单语言的改写润色用 paper_writing。」
- output_check 草案：「引用标记 [n]、数字、公式、变量名与原文逐一对应零改动；术语与 term_compass 产物（若有）一致；无漏译段落；译后自检列出主要改动点。」

**工作流**：确认方向与语体（中→英 journal 体 / 英→中）→ 有会话术语表则先注入（没有且术语密集时建议先跑 term_compass）→ 分段翻译 → 译后自检（时态/单复数/中式英语或欧化中文）。

### 7.2 P2 可选项

- `title_tuner`（指令技能，requires: [attachments]）：标题候选打磨（具体化、含方法名与任务）+ 关键词选择 + 摘要四要素改写建议；摘要长度等硬指标仍归 check_structure/check_format。
- `draft_delta`（原子工具，纯 difflib 确定性，requires: 两个 attachments）：两版草稿章节级文本对比 → 修改清单卡片，用于"给导师汇报改了什么"。

## 8. 系统工程配套

- **技能递名片扩展**（`agents/tools_impl.py::_SKILL_HINTS`）：search_papers→frontier_pulse/field_census/bib_import；deep_read→study_notes/exhibit_index/term_compass；write_review→integrity_sweep；draft_review→evidence_anchor/venue_radar/cover_pitch。注册表校验与"已加载不重复提示"机制复用现有实现。
- **系统 prompt**：L2 工具决策小节补 6 个新工具的触发/不触发描述（尤其 field_census vs research_map、integrity_sweep 的触发时机）；L3 推荐主线延长为「…→ 综述 → 写作 → 评审 → integrity_sweep → venue_radar → cover_pitch →（被审后）rebuttal_planner」。技能小节无需改代码（`skills_prompt_section()` 自动列出）。
- **调度回归**（`tests/eval/dispatch_golden.yaml`）：每个新技能 ≥2 条正例；新增边界负例——"这段话该引哪些文献"→evidence_anchor 而非 draft_review；"帮我回复审稿人"→rebuttal_planner 而非 paper_critique；"这个领域每年发文量趋势"→field_census 工具直调（skill: null）；"帮我写投稿信"→cover_pitch 而非 paper_writing；"翻译成英文"→scholar_translate 而非 paper_writing。
- **前端**：6 张新工具卡片（前沿简报/图表导览/可靠性质检/导入报告/领域普查/投稿去向）+ 标签注册；卡片沿用现有 CardHeader + memo 模式，图谱类可视化沿用纯 SVG 零依赖原则。
- **done 事件瘦身**：`_lite_tool_calls` 大字段清单补 new_papers/exhibits/report/venues/yearly 等新产物字段。
- **会话持久化**：`ChatSession` 按需增量字段（frontier 简报、术语表），旧历史兼容（缺字段默认空）。
- **配置**：`config/settings.yaml` 仅 frontier_pulse 时间窗默认值进 args 默认值，不新增配置节；OpenAlex/Crossref 复用现有限流与邮箱策略。
- **文档同步**（项目惯例：改功能同步重写对应章节）：DESIGN.md §4.2 工具表、§4.5 技能体系、§5 搜索子系统（日期过滤）、§9 地图（区分 census）、§11 前端卡片清单；README 功能列表。
- **测试纪律**：全部新代码走"纯函数 + stubbed-LLM，无真实 API"惯例；每工具至少：schema 校验、守卫、核心纯函数、降级路径。

## 9. 实施顺序（按依赖与价值）

- **阶段一（可靠性与证据，P0 核心）**：integrity_sweep → evidence_anchor → bib_import。理由：质检与证据是写作正确性刚需，且全部为确定性/低预算，风险最小。
- **阶段二（生命周期后半程，P0）**：frontier_pulse → venue_radar → rebuttal_planner → cover_pitch。理由：补齐"投稿"整段缺口。
- **阶段三（阅读与成稿体验，P1）**：exhibit_index → study_notes → term_compass → scholar_translate → proposal_architect → field_census。
- **P2 可选**：title_tuner、draft_delta，视反馈再做。
- 每阶段闭环：工具/技能落地 + 前端卡片 + golden set 扩充 + 单测 + DESIGN/README 同步重写。

## 10. 既有遗留方向（沿自上一版计划，仍未做）

- 多模态模型接入后实现 MediaAdapter（image/audio 解析）
- 多 worker 部署时的会话状态外置（当前单 worker 约束）
- Eval 黄金集持续扩充与定期回归
- Word 二进制排版级修改（字体/页边距写入 docx 样式）

## 11. 明确不做（边界声明）

- 跨会话长期记忆 / 个人文献库（设计红线，bib_import 也只进会话级）
- 论文正文代写、代做作业（诚信红线；一切起草以占位符处理经验事实）
- 付费墙全文的任何获取路径（OA-only 不动摇）
- 投稿系统对接、查重系统对接（无官方免费 API，不绕路）
- 图像内容理解（纯文本模型；exhibit_index 只做 caption 文本提取）
