# Paper Agent

> **论文调研智能体** — 对话驱动的文献检索、深读、研究地图、阅读路径与综述写作。可作为 OpenAI 兼容服务接入清小搭智能体广场。

> 本文档只记录当前版本的功能与用法，不写历史沿革。架构细节见 [docs/DESIGN.md](docs/DESIGN.md)。

## 功能

对话（`/chat`，唯一入口）中自然语言驱动全部能力，Agent 自主调度 13 个原子工具 + 技能层：

- **search_papers** — 意图理解 → 研究方向拆解 → 多源检索（OpenAlex / Semantic Scholar / arXiv / Crossref / Europe PMC / DOAJ / HAL / OpenAIRE / CORE，全部免费合法官方 API）→ 本地嵌入语义重排 → 自适应分层（核心集 / 候选集，无需手动设置数量）。**检索完成前对每篇论文轻量探测 OA PDF 是否可访问**（只读取响应头/PDF 文件头，不下载全文；`fulltext_status`: available / unavailable / unknown；源 API 的 `pdf_url` 绝不直接当作“可获取全文”，付费墙落地页会被判为仅摘要），前端检索卡片逐篇显示「全文可获取 / 仅摘要 / 待验证」。探测完成后执行 **best-effort 核心层全文保障**：若核心层已验证可读论文不足 5 篇，就按相关性将候选层中已验证 `available` 的论文提升进核心层，目标为 `min(5, 本次全部可读论文数)`；原核心论文保留、核心可扩容、候选删除提升项但不补位。会话已有论文时，用户点名其中某篇（DOI/标题）不会重复检索，而是直接走 deep_read / ask_papers
- **deep_read** — 结构化深读（`core/reading_policy.py`）：**默认对所选网络论文逐篇尝试全文级深读**，全文只走合法 OA 渠道；取不到 OA 全文的论文自动回退摘要级，并在工具结果中明确列出「未取得全文」清单，绝不把摘要冒充全文。核心集与候选集 id 均可直接传入；用户点名某篇（DOI/标题）时直接深读，不会重新检索。focus 作为上传附件按需理解的裁剪依据；网络论文无论是否传 focus 都会尝试全文。上传的 PDF / DOCX / PNG / JPG / WebP 也可深读，布局、OCR 与视觉理解只在需要时启动并复用 fingerprint / 图像哈希缓存；无 VLM 时优雅降级为文本与 caption。深读结果额外保存轻量 `section_outline` 与 `document_info`（下载、解析器、页数、字符数、章节数、扫描/OCR、图表理解计数），前端不会展示或复制完整正文
- **ask_papers** — 会话级 RAG 问答：章节目录/“有哪些部分”问题会注入受保护的完整 `section_outline` 证据，不会被普通 top-k 精排淘汰；其他问题继续走查询改写 → 向量 + BM25 混合检索（RRF 融合）→ 本地 cross-encoder 精排 → 父子扩展；答案带引用来源并经防幻觉校验。上传文档与图片留在当前会话附件域，图/表/公式问题会按需补充多模态理解，不会伪装成网络论文
- **research_map** — 研究地图：主题聚类 + 领域脉络 + 时间脉络 + **论文谱系图**（真实引用边来自 OpenAlex；固定画布确定性布局、节点详情面板、思想源流高亮、「深问这篇」直达问答）。谱系图节点的「全文可获取」标记沿用 search_papers / deep_read 的**已验证状态**，节点悬停与详情面板均显示；不再按元数据 `pdf_url` 猜测
- **reading_path** — 推荐阅读路径（奠基 → 桥梁 → 前沿，每篇附理由）
- **write_review** — 文献综述：按主题簇组织，引用经防幻觉校验并渲染为标题链接
- **check_structure** — 上传草稿结构体检（`tools/writing/structure_check.py`，纯代码零模型消耗）：章节树（markdown/数字/中文/LaTeX 标题）/ IMRaD 缺失章节 / 章节比例失衡 / 摘要长度 / 引用卫生 / 图表统计，前端专用卡片渲染体检报告
- **check_format** — 格式检查（纯代码）：图表编号连续性与正文引用、引用风格混用、GB/T 7714 规范度、关键词数量、标题断号；LaTeX 源查 \cite/\ref/参考文献块配对；支持传入用户格式要求逐条对照，排版项诚实列入人工核对清单
- **export_manuscript** — 写作产物导出：初稿/润色稿/修改清单 → docx / tex（ctexart 中文可编译）/ md 下载文件，清小搭侧经 x_soda 附件下发
- **清小搭展示边界** — 自有前端的 React 工具卡和可交互谱系图不会随 OpenAI 协议跨端执行；清小搭稳定获得正文/推理与 `x_soda.attachments` 文件卡。研究地图额外导出 Markdown 报告和静态 SVG 谱系图，使主题簇与图关系可在清小搭查看；交互筛选、缩放、节点“深问”仍需打开本项目自有前端
- **integrity_sweep** — 可靠性质检（纯官方 API，零模型）：逐篇查撤稿（OpenAlex `is_retracted`）/ 勘误或关切声明（Crossref `relation`）/ arXiv 预印本是否已有正式版；写综述、投稿导出前必跑
- **bib_import** — 导入 .bib 文献库（Zotero/EndNote/Mendeley 导出）到候选集，DOI 经 Crossref 自动补全，与会话论文去重后并入；与 citation_export 双向互通
- **exhibit_index** — 图表导览：列出网络论文或上传 PDF / DOCX / 图片里的图、表、公式（编号/类型/页码/缩略图）；上传附件按需解析并复用缓存，点击元素可继续追问
- **field_census** — 领域宏观计量（OpenAlex `group_by` 聚合 + 1 句画像）：近 15 年年度发文趋势、高产学/机构/期刊 top10；与 research_map 互补（前者看整个领域，后者看你手里这批论文）

技能层（`skills/builtin/`，Anthropic Agent Skills 模式：元数据常驻 prompt、完整指令按需加载、会话内去重、声明式前置门控、完成前自检契约）：

- **compare_papers** — 指令技能：多论文结构化横评表格（按论文类型自适应维度，无据写"未报告"）
- **research_gap** — 指令技能：分维度空白归纳 + 谱系结构信号 + 新颖性回验 + 选题评估（重复风险直判）
- **paper_critique** — 指令技能：审稿人式单篇批判（主张 vs 证据、可复现性清单、给作者的问题清单）
- **presentation_prep** — 指令技能：组会汇报大纲（时间分配 + 关键数字 + 死亡提问预案）
- **draft_review** — 指令技能 v3：上传草稿评审，**先并行 check_structure + check_format 拿确定性事实**再逐节定性评审 + 相关工作漏引对照（导师视角，建议可执行）
- **related_work** — 指令技能：相关工作章节写作（按流派组织、收尾落到用户研究位置）
- **paper_writing** — 指令技能 v2：论文写作教练四模式（逐章框架 / 初稿起草（数据一律占位符，诚信红线）/ 段落批改 / 语言润色；有会话论文时作范文对照）
- **topic_advisor** — 指令技能：选题方向指导（聚焦 → 新颖性取证 → 可行性 → 2-3 个带文献锚点的方向变体）
- **structure_advisor** — 指令技能：按论文类型推荐章节骨架 + 篇幅比例 + 章间逻辑
- **format_compliance** — 指令技能：按用户给定格式要求逐条对照整理（排版项转人工核对清单，可导出规范稿）
- **evidence_anchor** — 指令技能：任意草稿段落的逐句证据核对（拆出可证伪主张 → 逐条 ask_papers 取证 → "原文句→主张→证据与强度→建议插入位置"对照表，无证据主张明确标注不硬凑）
- **citation_export** — 可执行技能：BibTeX / GB/T 7714（国标）双格式导出，Crossref 按 DOI 补全卷期页
- **export_report** — 可执行技能：研究地图/综述导出为可下载 markdown 文件

其他特性：

- ReAct 循环 + 原生 function-calling（支持并行调用）。provider 原生 `reasoning_content`、显式 `<thinking>` 与工具调用前说明会实时进入思考折叠块并保存到历史；`use_skill` 仍为内部加载事件，不显示工具卡、开始/结果事件或公开工具调用记录；支持中断、复制、重新生成
- 会话内保留上下文（超长自动压缩摘要）；**跨会话无长期记忆**（文本向量按 session 隔离，上传元素检索还会额外按本会话附件 id 收敛）
- 文件上传支持 PDF / DOCX / TEX / TXT / MD / BIB / PNG / JPG / WebP：原件与提取文本 sidecar 分开保存；上传阶段零 VLM，扫描 PDF、DOCX 内嵌图片及独立图片在深读或图表问题时按需理解；图片可在右侧栏鉴权预览
- **用户数据隔离**：登录账号/游客身份隔离对话历史、上传原件/sidecar、上传文档元素资产和 Web 导出文件；附件 id、历史文件名即使被猜到也不会跨账号读取。会话 RAG 仍按 `session_id` 与当前论文/附件集合过滤；OpenAI-compatible API 使用独立 `data/openai_api/` 私有存储，公共下载别名仅用于清小搭短期拉取。
- **Git 与部署数据边界**：`data/`、`backend/data/`、`history_record/`、前端构建产物和本地数据库/PDF/上传/缓存均被 `.gitignore` 排除；云部署只同步源码，不上传开发机旧对话、旧论文、旧上传或旧数据库，服务器首次启动创建全新的运行数据。
- Prompt 注册表（`core/prompts/registry.py`）：全部 prompt 带版本号，trace 可溯源
- 中英双语 UI；「纸墨书院」设计风格（宣纸底 + 黛青主色 + 朱砂点缀）
- Eval 质量护栏：`tests/eval/` 黄金集，改 prompt / 换模型后对比检索召回率
- 数据合规：九个数据源 + Unpaywall 均为官方免费 API，全文 OA-only（付费墙无代码路径）；邮箱参数策略=无配置则匿名/不调用，绝不发占位身份；每源进程级限流对多用户部署天然合规。详见 [Official_Paper_Platform_License_Description.md](Official_Paper_Platform_License_Description.md)

## OpenAI 兼容端点（清小搭接入）

- `GET /v1/models`、`POST /v1/chat/completions`（真流式 SSE + 非流式 JSON），严格实现 `openai-compatible-agent-integration-guide.md`；`stream` 只接受 JSON 布尔，支持缺失/空/null `model` 与 `max_tokens:1`
- Bearer 鉴权支持管理员创建的长期 Agent API Key（`pa_live_...`，数据库仅存 SHA-256，完整值仅创建时显示一次，可撤销）；`AGENT_API_KEY` 保留为迁移/应急凭证。生产无任何密钥返回 503，错误或已撤销密钥返回 401
- 多轮对话：credential/user/message-chain HMAC alias + 7 天结构化 Checkpoint；重启恢复论文、摘要与 RAG，完整 messages/reasoning 不落盘
- 多模态输入：支持 OpenAI content 数组——`file` 与 `image_url`（URL 或 data URI）统一注册为会话附件，上传阶段只做快速提取，后续问答/深读按需调用视觉理解并缓存；URL 下载保留 SSRF 防护与 50MB 上限。`input_audio` 当前明确降级为不支持音频解析
- 文件产物输出：研究地图 / 综述可生成为 markdown；`export_manuscript` 可导出 md / docx / tex，下载路由同时支持 `.txt` 文本产物。所有文件均由 `GET /files/{name}` 下载，长中文文件名受 basename 与后缀白名单保护并可正常获取
- 接入向导：`baseUrl = https://你的域名/v1`，`credential = 管理员创建的长期 Agent API Key`；附件 URL 由 `PUBLIC_BASE_URL` 生成

## 多用户账号（自有前端公开部署时开启）

- `.env` 设 `AUTH_REQUIRED=true` 后启用账号体系；`GUEST_ACCESS=true` 时未登录浏览器按 `X-Guest-Id` 隔离，生产推荐 `GUEST_ACCESS=false` 强制登录。登录账号、游客和不同浏览器数据互不可见
- 历史记录按账号隔离（跨账号访问一律 404）；`REGISTRATION_OPEN=false` 可关闭公开注册；密码 PBKDF2 60 万次加盐哈希，浏览器令牌只存 SHA-256（SQLite `data/users.db`）
- `scripts/bootstrap_administrator.py` 交互式幂等初始化管理员；管理员页面 `/admin/agent-keys` 可管理清小搭长期密钥和修改密码（改密后撤销全部浏览器令牌）
- 默认关闭 = 本地单用户模式，`start.sh` 本地开发零配置；清小搭渠道（`/v1`）使用独立 Agent API Key，不使用浏览器登录令牌

## 快速开始

### 环境要求

- Python 3.11+（推荐 Miniconda）
- Node.js 18+ 和 pnpm
- DeepSeek API Key（或任意 OpenAI 兼容平台）

### 安装

```bash
cd Paper_Agent

# 创建 conda 环境（只在第一次做）
conda create -p .env_conda python=3.11 -y --solver=classic
conda activate ./.env_conda

# CPU 环境先安装官方 CPU-only PyTorch，避免 Linux 默认轮子拉取 CUDA 运行库
pip install -r requirements-cpu.txt

# 安装受 constraints.txt 约束的已验证依赖组合；后端依赖一并装上
pip install -r requirements.txt -e backend

# 安装前端依赖（只在第一次做）
cd frontend && pnpm install && cd ..

# 配置 API Key
cp .env.example .env
# 编辑 .env，取消注释你选择的方案，将 **** 替换为真实 Key
```

首次使用语义重排 / RAG 时会自动下载本地嵌入模型（~120MB）与 cross-encoder 精排模型 `BAAI/bge-reranker-base`（~1.1GB）（sentence-transformers，各仅需一次，CPU 运行）。国内服务器可设 `HF_ENDPOINT=https://hf-mirror.com` 加速。精排模型可在 `config/settings.yaml` 的 `storage.reranker_model` 置空关闭（回退 RRF 原序）。

### 运行

```bash
./start.sh            # 一键拉起前后端（开发模式）
./start.sh prod       # 生产模式（next build && next start + uvicorn 单 worker）
./start.sh backend    # 仅后端 http://127.0.0.1:8000（API 文档 /docs）
./start.sh frontend   # 仅前端 http://localhost:3000
./start.sh stop       # 停止所有
```

### 测试

```bash
./.env_conda/bin/python -m pytest tests/ -q   # 默认非 slow；stub 外部 API/LLM/VLM
cd frontend && pnpm build                     # 前端类型检查 + 构建
```

真实 API / 浏览器 E2E 不进入普通 CI。发布前可启动 `./start.sh` 后从 `http://127.0.0.1:3000/chat` 手工或用 Playwright 验收：原生 reasoning、显式 `<thinking>` 或工具调用前说明在思考折叠块中增量显示，且 `use_skill` 不产生公开工具卡/开始/结果事件；上传图片和含内嵌图的 DOCX 能按需理解；长中文 `.docx` 点击下载返回 200，文件为以 `PK` 开头的有效 OOXML ZIP。不要把 API key、真实附件、`history_record/`、运行时 `data/`、截图或 `.next` 提交到仓库。

## 清小搭 / OpenAI API 独立存储

`/v1/models` 与 `/v1/chat/completions` 使用独立 `data/openai_api/` 根目录，不会改变自制前端的历史、上传、论文、附件、图谱和长期保留。默认策略：7 天结构化 Checkpoint/私有上传、24 小时导出、3 天公共 PDF、90 天语义/视觉缓存、API Trace 关闭。公共 PDF 过期即由计划清理删除，后续需要时 deep_read 会自动重新下载并提取。完整 messages、reasoning/思维链、API Key、raw bytes 和完整 PDF 正文不会写入 Checkpoint。

管理员页面：`/admin/api-storage`。可查看分类容量、磁盘状态、清理记录，选择 privacy/balanced/performance、自定义 TTL、95% pause/emergency 和 off/metadata/full Trace；每项都有隐私、磁盘、延迟、费用、连续性、重下载/OCR/VLM、生效与恢复默认说明。缩短 TTL、Full Trace、紧急删除、立即清理和遗留扫描必须预览并二次确认。

管理员页面：`/admin/accounts-data`。统一列出各 web 账号和 Agent API Key 的历史会话、上传文件、Trace、会话向量、API 私有文件占用；管理员可**彻底删除**某个账号的全部数据（文件先覆写再删除、SQLite secure_delete + VACUUM，无法恢复），也可一键清理共享的论文 PDF/元素资产缓存。

```bash
# API-only hourly cleanup；生产应安装 deploy/systemd 中的 service/timer。
./.env_conda/bin/python scripts/cleanup_openai_api_storage.py --scheduled --reconcile
```

75/85/95/98% 磁盘保护下，普通文字问答继续；98% 文件重任务保护不可关闭。`/files/{alias}` 只暴露短期公开 alias，不暴露物理 hash 路径。仅向清小搭提供服务时 2C4G 可用于轻量起步（重任务并发 1）；3–6 人混合深读的正式建议仍为 4C16G，详见部署手册。

## 服务器部署

**新手请直接按 [Website_deployment_plan.md](Website_deployment_plan.md) 逐步操作**（买 ECS → 安全组 → 装环境 → 密钥 → systemd → nginx/HTTPS → 清小搭向导，每步带解释）。以下是架构要点速览：

架构无外部服务依赖（SQLite / Chroma / JSON 全在本地文件），从 localhost 迁移到服务器只需环境与反向代理配置：

1. **环境变量**（`.env`）：
   - `FRONTEND_ORIGIN` / `CORS_ORIGINS` — 前端公网域名（CORS 白名单）
   - `NEXT_PUBLIC_BACKEND_URL` — 后端公网 Origin（前端 SSE 直连用，**构建时**注入：`BACKEND_URL=... NEXT_PUBLIC_BACKEND_URL=... pnpm build`）
   - `PUBLIC_BASE_URL` — 清小搭附件使用的公网 HTTPS Origin
   - 管理员创建的长期 Agent API Key — 清小搭凭证；`AGENT_API_KEY` 仅作迁移/应急备用
   - `AUTH_REQUIRED=true`、`REGISTRATION_OPEN=false`、`GUEST_ACCESS=false` — 正式自有前端策略
   - `APP_HOST` / `APP_PORT` — 后端绑定地址与端口
2. **反向代理**（nginx / caddy）：终结 HTTPS；代理 `/api`、`/v1`、`/files` 到后端；SSE 需要 `proxy_buffering off` 和 `proxy_read_timeout ≥ 300s`。
3. **单进程约束**：会话内存、熔断器、限流信号量均为进程内状态，uvicorn 必须 `--workers 1`（`start.sh prod` 已内置）。
4. **本地模型**：嵌入模型 ~120MB + 精排模型 ~1.1GB 首次加载需联网下载；可预置 `HF_ENDPOINT` 或提前下载到 `~/.cache` 打包；精排不可用只影响排序质量，不阻塞服务。
5. **数据持久化**：`data/`（SQLite / Chroma / 上传原件与 `.txt` sidecar / 上传元素裁图 / 产物）与 `history_record/` 挂卷或定期备份。
6. **进程管理**：systemd 两个 unit（uvicorn + next start），`restart=always`。

清小搭上线路径：交互式初始化管理员并创建长期 Agent API Key → 在接入向导填 `baseUrl=https://你的域名/v1` + `credential` → 探测 4 项（连通性 / 凭证 / 最小对话 / 响应格式）→ 真实试聊 → 审核上架。正式 3–6 人部署规格、systemd/nginx 配置与容量验收见完整手册。

## 项目结构

```
backend/app/     FastAPI：/api/v1/chat/*（自有前端）+ /v1/*（OpenAI 兼容）+ /files/*
agents/          orchestrator（ReAct 循环）· session · chat_tools · tools_impl
                 search_agent · reader_agent · review_agent · map_agent
core/            llm · prompts/（注册表）· embeddings · API checkpoint/artifact/cleanup/pressure
                 tool_protocol · circuit_breaker · trace · history_store · storage_context · config · models
tools/           search/（9 数据源 + OpenAlex 引用边）· pdf/（OA-only 下载 + Unpaywall）· ingest/（URL 下载 + 上传附件注册/按需多模态）
                 retrieval/（结构化切块 · BM25+RRF · cross-encoder 精排）· storage/（SQLite · 会话级向量库 · 谱系图数据）· export/
frontend/        Next.js chat 单页：对话 + 工具卡片 + 谱系图 v2（确定性布局纯函数 + 详情面板）
tests/           pytest（纯函数 + stubbed LLM/VLM/Docling）+ eval/ 黄金集
```
