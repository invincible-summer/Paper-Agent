# Paper Agent

> **论文调研智能体** — 对话驱动的文献检索、深读、研究地图、阅读路径与综述写作。可作为 OpenAI 兼容服务接入清小搭智能体广场。

> 本文档只记录当前版本的功能与用法，不写历史沿革。架构细节见 [docs/DESIGN.md](docs/DESIGN.md)。

## 功能

对话（`/chat`，唯一入口）中自然语言驱动全部能力，Agent 自主调度 13 个原子工具 + 技能层：

- **search_papers** — 意图理解 → 研究方向拆解 → 多源检索（OpenAlex / Semantic Scholar / arXiv / Crossref / Europe PMC / DOAJ / HAL / OpenAIRE / CORE，全部免费合法官方 API）→ 本地嵌入语义重排 → 自适应分层（核心集 / 候选集，无需手动设置数量）。**检索完成前对每篇论文轻量探测 OA PDF 是否可访问**（只读取响应头/PDF 文件头，不下载全文；`fulltext_status`: available / unavailable / unknown；源 API 的 `pdf_url` 绝不直接当作“可获取全文”，付费墙落地页会被判为仅摘要），前端检索卡片逐篇显示「全文可获取 / 仅摘要 / 待验证」。探测完成后执行 **best-effort 核心层全文保障**：若核心层已验证可读论文不足 5 篇，就按相关性将候选层中已验证 `available` 的论文提升进核心层，目标为 `min(5, 本次全部可读论文数)`；原核心论文保留、核心可扩容、候选删除提升项但不补位。会话已有论文时，用户点名其中某篇（DOI/标题）不会重复检索，而是直接走 deep_read / ask_papers
- **deep_read** — 结构化深读（`core/reading_policy.py`）：网络论文只走合法 OA 渠道，并遵守管理员的四档全文拉取策略（完全开启 / 仅明确深读时拉取 / 仅探测不下载 / 完全关闭远程全文）；本地缓存始终可复用。没有真实全文时只按摘要级处理，绝不把摘要冒充全文。核心集与候选集 id 均可直接传入；上传的 PDF / DOCX / PNG / JPG / WebP 不受网络论文拉取开关影响，布局、OCR 与视觉理解按需启动并复用缓存。
- **ask_papers** — 会话级 RAG 问答：章节目录/“有哪些部分”问题会注入受保护的完整 `section_outline` 证据，不会被普通 top-k 精排淘汰；其他问题继续走查询改写 → 向量 + BM25 混合检索（RRF 融合）→ 本地 cross-encoder 精排 → 父子扩展；答案带引用来源并经防幻觉校验。上传文档与图片留在当前会话附件域，图/表/公式问题会按需补充多模态理解，不会伪装成网络论文
- **research_map** — 研究地图：主题聚类 + 领域脉络 + 时间脉络 + **论文谱系图**（真实引用边来自 OpenAlex；固定画布确定性布局、节点详情面板、思想源流高亮、「深问这篇」直达问答）。谱系图节点的「全文可获取」标记沿用 search_papers / deep_read 的**已验证状态**，节点悬停与详情面板均显示；不再按元数据 `pdf_url` 猜测
- **reading_path** — 推荐阅读路径（奠基 → 桥梁 → 前沿，每篇附理由）
- **write_review** — 文献综述：按主题簇组织，引用经防幻觉校验并渲染为标题链接
- **check_structure** — 上传草稿结构体检（`tools/writing/structure_check.py`，纯代码零模型消耗）：章节树（markdown/数字/中文/LaTeX 标题）/ IMRaD 缺失章节 / 章节比例失衡 / 摘要长度 / 引用卫生 / 图表统计，前端专用卡片渲染体检报告
- **check_format** — 格式检查（纯代码）：图表编号连续性与正文引用、引用风格混用、GB/T 7714 规范度、关键词数量、标题断号；LaTeX 源查 \cite/\ref/参考文献块配对；支持传入用户格式要求逐条对照，排版项诚实列入人工核对清单
- **export_manuscript** — 写作产物导出：初稿/润色稿/修改清单 → docx / tex（ctexart 中文可编译）/ md 下载文件，清小搭侧经 x_soda 附件下发
- **使用文档公告页** — `/usage-doc` 始终公开只读展示管理员维护的 Markdown 功能说明；管理员登录后可在同一页面的单个文本框中编辑并保存，使用“上传图片并插入”按钮把 PNG/JPEG/GIF/WebP 上传到受控文档资源目录并插入当前光标位置。
- **清小搭展示边界与卡片仿真** — 自有前端的 React 工具卡和可交互谱系图不会随 OpenAI 协议跨端执行；`/v1` 通道以接口文档允许的三个表面复刻视觉效果：正文 **一行式工具状态行**（`tools/export/cards.py`，每个工具完成后插入一行状态摘要，如 `🔎 文献检索 · 核心集 12 篇 / 候选 13 篇`，插在回答之前，节省被回显进下一轮上下文的体积）、**检索结果规范化表格**（文献检索完成后确定性生成完整论文清单 markdown 表格：核心层/候选层、年份、被引、全文可取性、DOI/来源链接，不受卡片开关控制）、思考折叠中的 **emoji 进度与技能加载提示**（`📘 已加载技能《…》` + 正文 `━━ 📘 技能 · … ━━` 行）、以及 `x_soda.attachments` **文件卡片**（研究地图 SVG、可选 Markdown 关系说明、元素裁剪图 PNG、BibTeX 文件、领域普查趋势图 SVG）。管理员可在 `/admin/display-policy` 配置工具状态行、技能行与研究地图渲染策略：`legacy_svg` 默认保留原版 Markdown + SVG，`pretty_svg` 使用自包含、可缩放的美化 SVG，`pretty_svg_markdown` 再附一份不依赖 Mermaid 的引用关系清单；交互筛选、缩放、节点“深问”仍需打开本项目自有前端
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
- 数据合规：论文渠道按官方 API/OAI/元数据许可分为开放、条件启用和暂不接入；全文 OA-only（付费墙无代码路径）；CORE/Semantic Scholar 需要额外许可门禁；bioRxiv/medRxiv 只通过官方 metadata API 建立本地索引；ChinaXiv 暂不接入。详见 [Official_Paper_Platform_License_Description.md](Official_Paper_Platform_License_Description.md)

## OpenAI 兼容端点（清小搭接入）

- `GET /v1/models`、`POST /v1/chat/completions`（真流式 SSE + 非流式 JSON），严格实现 `openai-compatible-agent-integration-guide.md`；`stream` 只接受 JSON 布尔，支持缺失/空/null `model` 与 `max_tokens:1`
- `/v1` 流式请求采用管理员可调的整轮软时限（默认 95 秒，可在 `/admin/performance` 调整为 30–100 秒）与固定 105 秒硬截止：验证后先发送 role 首帧，工具期间持续发送 reasoning 进度，超时或客户端中断都会安全取消并闭合为唯一 stop 帧 + `[DONE]`；每轮只读取一次工具预算策略快照，固定硬截止至少为协议收尾保留 5 秒。微小 token 会按时间/字数合帧，避免逐字符 SSE 产生数十倍协议开销。服务器侧诊断可运行 `scripts/diagnose_openai_agent_stream.py`
- Bearer 鉴权支持管理员创建的长期 Agent API Key（`pa_live_...`，数据库仅存 SHA-256，完整值仅创建时显示一次，可撤销）；`AGENT_API_KEY` 保留为迁移/应急凭证。生产无任何密钥返回 503，错误或已撤销密钥返回 401
- 多轮对话：清小搭 `sessionId` 经服务器 HMAC 后作为稳定 Checkpoint 主别名，并在首轮开始时立即绑定，因此流式中断后也能恢复已经保存的论文集等部分状态；原始 `sessionId` 不落库，别名继续按 Agent API credential 隔离。缺失 `sessionId` 时回退到 credential/user/message-chain HMAC alias
- 每轮都从请求携带的完整可见 `messages` 重建临时文本历史（排除当前最后一条 user），所以“可以 / 继续”等短确认能读取上一轮助手的明确提议；Checkpoint 仍只保存论文、摘要、地图、附件引用等白名单结构化状态，完整 messages/reasoning 不落盘。调用方 `system` 指令会在服务端安全规则之后受限加入上下文，外部 `tool` 历史可安全忽略
- `/v1` 工具调度执行最少必要原则：一次模型决策批次最多实际执行一个公开工具，整轮最多启动三个；首个必要工具达到 5/10/15 秒（轻/中/重）最低可用时间时可按剩余预算裁剪，后续工具必须完整容纳管理员预算与收尾预留。任一 TIMEOUT 或 `budget_exhausted` partial 会关闭本轮后续工具，只总结已有证据；Web 通道原有并行安全工具行为不变
- 检索超时采用部分成功：多源检索保留已完成来源，时间紧时切换词项重合 + 被引 + 时效的确定性快速重排，分层后立即发布可恢复 snapshot；OA 全文探测、SQLite/Chroma 增强可跳过。`partial` 结果仍输出标准论文表、工具状态行并写入 Checkpoint，不会因后续增强超时丢弃论文列表
- 多模态输入：支持 OpenAI content 数组——`file.url` 存在时始终作为实际下载地址（与 `file_id` 同时存在也不例外），`file_id` 只保留为来源标识，绝不拼接成本地路径或猜测公网 URL；仅有 `file_id` 时不联网、不报 500，而是在本轮明确提示缺少可下载 URL。`file` 与 `image_url`（URL 或 data URI）统一注册为会话附件，HTTP(S) 下载逐跳执行 SSRF 公网校验。`/v1` 文件上限默认 200 MiB，可由管理员在 `/admin/api-storage` 下调但不能超过 200 MiB；Web `/chat/upload` 仍为 20 MiB。PDF/DOCX/TEX/TXT/MD/BIB/PNG/JPG/JPEG/WebP 按原能力处理；DOC/XLS/XLSX 可安全保存到 API 私有会话并生成空 sidecar，但标记为 `deferred`、当前不解析；PPT/PPTX 和其他未知格式继续拒绝。`input_audio` 当前明确降级为不支持音频解析
- 文件产物输出：研究地图按管理员策略生成原版或美化 SVG（`fileType: image`、`mimeType: image/svg+xml`），并可附普通 Markdown 研究报告/引用关系说明（`fileType: text`、`mimeType: text/markdown`）；美化 SVG 使用 `viewBox` 与 `preserveAspectRatio`，无脚本、外链资源或 Mermaid 依赖。综述可生成为 markdown；`export_manuscript` 可导出 md / docx / tex，下载路由同时支持 `.txt` 文本产物。所有文件均由 `GET /files/{name}` 下载，长中文文件名受 basename 与后缀白名单保护并可正常获取
- 富展示（按接口文档能力实现）：工具完成时正文插入一行式 Markdown 状态行；文献检索完成后确定性生成完整论文清单表格（核心/候选层、全文可取性、原文链接），不受卡片开关控制；技能加载触发思考折叠提示 + 正文技能行；研究地图 SVG、可选 Markdown 关系说明、`explain_element` 图表裁剪图、`citation_export` 的 .bib、`field_census` 趋势图 SVG 作为当轮附件卡片下发（image 类附件自动带 `previewUrl`）。管理员在 `/admin/display-policy` 配置两个开关和 `legacy_svg | pretty_svg | pretty_svg_markdown` 枚举（`api_display_policy` 单行表，schema v8，5 秒读缓存、乐观锁），存于 `data/openai_api/state.db`；默认 `legacy_svg` 保持升级前行为
- 接入向导：`baseUrl = https://你的域名/v1`，`credential = 管理员创建的长期 Agent API Key`；附件 URL 由 `PUBLIC_BASE_URL` 生成

## 多用户账号（自有前端公开部署时开启）

- `.env` 中的 `AUTH_REQUIRED` / `REGISTRATION_OPEN` / `GUEST_ACCESS` / `EMAIL_REQUIREMENT` 只在首次运行时写入运行时设置行（`data/users.db`），之后由管理员在 `/admin/auth-settings`（访问控制）页面随时修改，保存后立即生效、重启不回退；`.env` 仅为初始种子。`GUEST_ACCESS=true` 时未登录浏览器按 `X-Guest-Id` 隔离。登录账号、游客和不同浏览器数据互不可见
- 关闭账号登录（高危，需输入「确认」二次确认）会让所有未登录访问立即变为本地用户并穿透数据隔离；误关后用 `scripts/enable_auth_required.py` 恢复（约 5 秒生效，无需重启）
- 历史记录按账号隔离（跨账号访问一律 404）；注册邮箱要求三档可选：不要求 / 仅填写 / 邮箱 + 6 位验证码（需在 `.env` 配置 `SMTP_*`，管理页可发测试邮件验证；验证码 10 分钟有效、60 秒重发冷却、同邮箱唯一绑定）。管理员账号豁免：邮箱可留空、无需验证。密码 PBKDF2 60 万次加盐哈希，浏览器令牌只存 SHA-256（SQLite `data/users.db`）
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
# backend/pyproject.toml 会自动安装上传接口必需的 python-multipart。
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

`/v1/models` 与 `/v1/chat/completions` 使用独立 `data/openai_api/` 根目录，不会改变自制前端的历史、上传、论文、附件、图谱和长期保留。默认策略：7 天结构化 Checkpoint/私有上传、24 小时导出、3 天公共 PDF、90 天语义/视觉缓存、API Trace 关闭，以及单文件 200 MiB 的远程下载/私有保存上限。公共 PDF 过期即由计划清理删除，后续需要时 deep_read 会自动重新下载并提取。完整 messages、reasoning/思维链、API Key、raw bytes 和完整 PDF 正文不会写入 Checkpoint。

管理员页面：`/admin/auth-settings`（访问控制）。运行时开关账号登录、游客访问、开放注册，三档选择注册邮箱要求（不要求 / 仅填写 / 邮箱 + 验证码），显示 SMTP 配置状态并支持发送测试邮件；带版本乐观锁，关账号登录需输入「确认」二次确认。各开关的说明都收在问号帮助弹窗里。


管理员页面：`/admin/paper-search`（论文检索）。可逐项启用/关闭 OpenAlex、Semantic Scholar、arXiv、Crossref、Europe PMC、DOAJ、HAL、OpenAIRE、CORE、bioRxiv、medRxiv、PubMed、DataCite、DBLP；默认智能路由按学科/意图选择最多 4 个主渠道，结果不足时最多 2 个兜底渠道，也可由管理员切换全启用模式。页面展示协议、许可门禁、配置、熔断、Rxiv 本地索引覆盖/日期与分页游标/同步错误，并提供真实检索连通性、arXiv 多查询负载和最多读取 1 MiB 的 PDF 下载测速；不接受任意 URL、不保存测速 PDF、不显示密钥或完整配置邮箱。默认单源预算 12 秒，检索总预算可在 10–30 秒调整且硬上限为 30 秒，全文探测默认 30 秒；达到时限返回部分结果。每个渠道卡显示综合熔断状态、最近延迟/错误与中文处置建议，并支持一键「熔断 300 秒 / 提前恢复」；管理员连通性检测的真实结果会回写渠道健康与熔断视图。

管理员页面：`/admin/api-storage`。可查看分类容量、磁盘状态、清理记录，选择 privacy/balanced/performance、自定义 TTL、1–200 MiB 的 API 远程文件上限、95% pause/emergency 和 off/metadata/full Trace；文件上限只影响后续 `/v1` 下载/私有保存，不影响 Web 20 MiB 上传，修改不会删除或重处理已保存文件。每项都有隐私、磁盘、延迟、费用、连续性、重下载/OCR/VLM、生效与恢复默认说明。缩短 TTL、Full Trace、紧急删除、立即清理和遗留扫描必须预览并二次确认。

管理员页面：`/admin/accounts-data`。统一列出各 web 账号和 Agent API Key 的历史会话、上传文件、Trace、会话向量、API 私有文件占用；管理员可**彻底删除**某个账号的全部数据（文件先覆写再删除、SQLite secure_delete + VACUUM，无法恢复），也可一键清理共享的论文 PDF/元素资产缓存。

```bash
# API-only hourly cleanup；生产应安装 deploy/systemd 中的 service/timer。
./.env_conda/bin/python scripts/cleanup_openai_api_storage.py --scheduled --reconcile
```

75/85/95/98% 磁盘保护下，普通文字问答继续；98% 文件重任务保护不可关闭。`/files/{alias}` 只暴露短期公开 alias，不暴露物理 hash 路径。仅向清小搭提供服务时 2C4G 可用于轻量起步（重任务并发 1）；3–6 人混合深读的正式建议仍为 4C16G，详见部署手册。

## 服务器部署

**生产部署与升级请按 [Website_deployment_plan.md](Website_deployment_plan.md) 操作**；手册随仓库维护，但只有用户明确确认某版本上云后，才填写末尾的版本专属发布命令。以下是架构要点速览：

架构无外部服务依赖（SQLite / Chroma / JSON 全在本地文件），从 localhost 迁移到服务器只需环境与反向代理配置：

1. **环境变量**（`.env`）：
   - `FRONTEND_ORIGIN` / `CORS_ORIGINS` — 前端公网域名（CORS 白名单）
   - `NEXT_PUBLIC_BACKEND_URL` — 后端公网 Origin（前端 SSE 直连用，**构建时**注入：`BACKEND_URL=... NEXT_PUBLIC_BACKEND_URL=... pnpm build`）
   - `PUBLIC_BASE_URL` — 清小搭附件使用的公网 HTTPS Origin
   - 管理员创建的长期 Agent API Key — 清小搭凭证；`AGENT_API_KEY` 仅作迁移/应急备用
   - `AUTH_REQUIRED=true`、`REGISTRATION_OPEN=false`、`GUEST_ACCESS=false` — 正式自有前端策略
   - `APP_HOST` / `APP_PORT` — 后端绑定地址与端口
2. **反向代理**（nginx / caddy）：终结 HTTPS；代理 `/api`、`/v1`、`/files` 到后端；SSE 需要 `proxy_buffering off` 和 `proxy_read_timeout ≥ 300s`。
3. **单进程约束**：会话内存、熔断器、限流信号量均为进程内状态，uvicorn 必须 `--workers 1`（`start.sh prod` 已内置）。`/v1` 与网页 `/api/v1` 共用事件循环；Docling、本地嵌入、CrossEncoder、Chroma 和附件提取进入单槽 ML worker，SQLite Checkpoint 与轻量文件 I/O 使用独立受限 I/O worker，避免重任务阻塞 SSE、网页首屏与健康检查。
4. **本地模型**：嵌入模型 ~120MB + 精排模型 ~1.1GB 首次加载需联网下载；可预置 `HF_ENDPOINT` 或提前下载到 `~/.cache` 打包；精排不可用只影响排序质量，不阻塞服务。前端 `/auth/config` 启动探测有 8 秒上限，后端异常时不会无限停留在加载动画；服务端鉴权仍是最终边界。
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

### 管理员性能策略与研究地图降级

管理员可在 `/admin/performance` 配置启动预热（`blocking`、`background`、`role_first`、`off`）和研究地图引文增强（`fast`、`quality`、`off`）。预热只做本地 Python 导入与客户端构造，不调用模型或下载文件；`role_first` 的流式 `/v1` 请求会先发送标准 `role` 帧，非流式请求仍需在处理时完成冷加载。地图引文由后端批量请求 OpenAlex，默认最多等待 3 秒，策略关闭或 OpenAlex 搜索源关闭时不会发起请求。

同一页面还提供「工具时限预算」：每个工具（中文名 + 用途说明 + 建议上限，不暴露裸 id）的单次调用预算可单独调整，区间 5–105 秒，另有作用于所有工具的整轮预留量（建议 8 秒）和清小搭 `/v1` 整轮软时限（30–100 秒，默认 95 秒）。实际生效值 = min(工具预算, 整轮剩余 − 预留量)；固定硬截止为 105 秒，超过当前 `/v1` 软时限的部分仅在 Web 通道（240/300 秒）生效。保存后下一次工具调用立即生效，无需重启。每行内联显示工具级熔断状态并可一键恢复。`search_papers` 内部各阶段（意图理解 LLM 10 秒子超时、多源检索、全文探测）共享该工具预算，探测预算自动钳制为剩余时间，检索成功后不会再因探测段超时作废整次调用。

研究地图的聚类和语义边共享一次嵌入，地图概述与领域脉络共享一次有预算的 utility 调用，并与可选引文增强并发。嵌入、LLM、OpenAlex 任一失败都会保留节点、时间线、Markdown/SVG 附件并返回降级状态；相同论文/全文状态/提示版本/引文策略指纹会跨轮复用地图。Web 渠道使用 240 秒软时限/300 秒硬时限，`/v1` 继续使用 95 秒/105 秒；`search_papers` 与 `research_map` 每轮最多实际开始一次，超时后当前轮不会再次调用。
