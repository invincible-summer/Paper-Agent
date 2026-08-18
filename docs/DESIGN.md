# Paper Agent 设计文档

> 本文档只描述**当前已落地**的架构与行为（以代码为准），不保留历史版本与决策编号。功能变更时同步重写对应章节。

---

## 1. 系统概述

本地运行的论文调研智能体（个人工具 + 可接入清小搭广场），前后端分离，**对话是唯一交互入口**。覆盖研究前期全流程：

```
研究目标 → 理解意图 → 拆解研究方向 → 多源检索 → 语义相关性重排
→ 摘要级/全文级深读 → 研究地图（主题簇+脉络+谱系图）→ 阅读路径 → 文献综述
```

刻意不做：跨会话长期记忆（每个对话相互独立）；CNKI/万方等无公开 API 的国内源（中文文献经 OpenAlex/Crossref 中文 DOI 覆盖，全文由用户上传 PDF 补充）；论文正文代写。

### 部署形态

- 后端：FastAPI（`backend/app`），uvicorn 单 worker（进程内状态约束，见 §8）
- 前端：Next.js 14（`frontend/`），`/chat` 单页，`/` 重定向到 `/chat`
- 启动：`./start.sh`（开发）/ `./start.sh prod`（生产构建）
- 密钥：LLM API Key 全程在本机 `.env`，不出本机

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层                                                        │
│  浏览器: Next.js /chat（唯一页面）                             │
│  清小搭: POST /v1/chat/completions（OpenAI 兼容，真流式 SSE）  │
└──────────────┬───────────────────────────────────────────────┘
┌──────────────▼───────────────────────────────────────────────┐
│ Backend (FastAPI, backend/app)                                │
│  /api/v1/chat/*  对话流式+历史CRUD+上传+文件取回（自有前端）   │
│  /v1/*           OpenAI 兼容层（models + chat/completions）    │
│  /files/{name}   产物文件下载（x_soda.attachments 目标）       │
│  SSE 直连/代理双通道；CORS 白名单；独立 Agent API Key 鉴权 /v1 │
└──────────────┬───────────────────────────────────────────────┘
┌──────────────▼───────────────────────────────────────────────┐
│ 编排层 (agents/)                                              │
│  orchestrator.chat_turn ← 唯一编排者：ReAct 循环 + 原生 FC     │
│  17 个 typed tool（tools_impl，清单见 §4.2）：                 │
│   search/deep_read/ask_papers/research_map/reading_path/        │
│   write_review/check_structure/check_format/export_manuscript/  │
│   integrity_sweep/bib_import/exhibit_index/explain_element/     │
│   field_census/use_skill/citation_export/export_report           │
│  agents: search(意图+重排) reader(提取) review(综述) map(地图) │
├──────────────────────────────────────────────────────────────┤
│ 检索层（会话级 RAG）                                          │
│  core/embeddings(本地 MiniLM) + tools/storage/vectorstore      │
│  (Chroma，session_id 隔离) + tools/retrieval/bm25(RRF 融合)   │
├──────────────────────────────────────────────────────────────┤
│ 工具层 (tools/)                                               │
│  search/  5 数据源 backend + manager(并发/去重) + openalex_refs│
│  pdf/     OA 下载 | Docling/PyMuPDF 结构解析 | 元素裁图         │
│  ingest/  URL 安全下载 + 附件原件/文本 sidecar + 按需多模态    │
│  storage/ database(SQLite 缓存) | vectorstore | genealogy      │
│  export/  bibtex | report(md 报告渲染)                         │
├──────────────────────────────────────────────────────────────┤
│ 基础设施 (core/)                                              │
│  llm(限流) | prompts/(注册表+版本) | session_memory(/v1 多轮)  │
│  tool_protocol | circuit_breaker | trace | history_store |     │
│  embeddings | config | models                                  │
└──────────────────────────────────────────────────────────────┘
```

### 项目文件结构

```
Paper_Agent/
├── backend/app/
│   ├── main.py                 # app 工厂 + CORS + 三个路由挂载
│   ├── core/config.py          # Settings(.env) + allowed_origins 派生
│   ├── api/v1/
│   │   ├── router.py           # /api/v1 聚合（health + chat）
│   │   ├── chat.py             # 对话 SSE + 历史 CRUD + 上传/文件取回
│   │   ├── openai_compat.py    # /v1 OpenAI 兼容层（清小搭）
│   │   ├── multimodal.py       # OpenAI content 数组 → 统一附件摄取
│   │   └── files.py            # /files/{name} 产物下载
│   └── schemas/chat.py
│
├── agents/
│   ├── orchestrator.py         # chat_turn ReAct 循环 + SSE 事件 + 历史压缩
│   ├── session.py              # ChatSession + 历史持久化
│   ├── chat_tools.py           # 17 个工具的 pydantic schema（单一事实源）
│   ├── tools_impl.py           # 17 个工具实现 + 校验 + 分发 + 规则反射器
│   ├── search_agent.py         # 意图理解 + 语义重排 + 自适应分层
│   ├── reader_agent.py         # PDF/摘要 → 结构化提取（并发+缓存+分段）
│   ├── review_agent.py         # 综述合成 + 防幻觉引用校验 + 引用渲染
│   ├── map_agent.py            # 聚类 + 簇标注 + 领域脉络 + 阅读路径
│   └── chat_agent.py           # 兼容门面（re-export，勿新增逻辑）
│
├── core/
│   ├── prompts/
│   │   ├── registry.py         # PromptDef(id, version, text) 注册表
│   │   ├── system.py           # 分层系统 prompt（L1-L6）
│   │   ├── search.py           # 意图理解 prompt
│   │   ├── map_path.py         # 簇标注/脉络/阅读路径 prompt
│   │   ├── reader_prompts.py / review_prompts.py / qa_prompts.py
│   ├── llm.py                  # RateLimitedLLM（每模型信号量 3）+ bind_tools 保限流
│   ├── session_memory.py       # /v1 会话缓存（TTL+LRU，见 §7）
│   ├── embeddings.py           # 本地 sentence-transformers 单例，失败降级
│   ├── tool_protocol.py        # ToolResult 统一协议 + ErrorCode
│   ├── circuit_breaker.py      # 进程级熔断（3 连败，冷却 300s）
│   ├── trace.py                # JSONL/HTML trace（call/result/decision 三链）
│   ├── history_store.py        # 会话 JSON 存储（basename 防穿越）
│   ├── config.py / models.py / state.py / i18n.py
│
├── tools/
│   ├── search/                 # openalex arxiv crossref europepmc doaj
│   │                           # (semantic_scholar 默认关) + manager + openalex_refs
│   ├── retrieval/bm25.py       # 纯 Python BM25（CJK bigram）+ RRF 融合
│   ├── pdf/                    # OA fetcher | structure(Docling + PyMuPDF fallback) | caption
│   ├── ingest/                 # downloader(URL/SSRF) + attachments(统一上传与按需理解)
│   ├── storage/                # database | vectorstore | genealogy
│   └── export/                 # bibtex | report
│
├── frontend/                   # Next.js 14 chat 单页（纸墨书院设计令牌）
│   ├── app/                    # page.tsx(重定向) chat/page.tsx layout globals.css
│   ├── components/             # AppShell/Nav/Sidebar/RightSidebar/SettingsPopover
│   │                           # GenealogyGraph(SVG 谱系图) chat/(ChatInput/ChatMessage)
│   ├── stores/                 # chat(会话+偏好) ui(边栏)
│   └── lib/                    # chat-api(SSE) history-loader types i18n
│
├── tests/                      # pytest：纯函数 + stubbed-LLM（无真实 API）
├── config/settings.yaml        # 可调参数（.env 覆盖部分项）
├── history_record/             # 会话 JSON + trace/（gitignored）
├── data/                       # chroma/ pdfs/ uploads/ exports/ metadata.db（gitignored）
└── docs/DESIGN.md              # 本文档
```

---

## 3. 后端 API

### 3.1 对话端点（`/api/v1/chat`）

开启 `AUTH_REQUIRED` 后全部端点要求 `Authorization: Bearer <登录令牌>`（§3.4），历史记录按账号隔离；默认关闭 = 本地单用户模式（内置 `local` 用户，行为与旧版一致）。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/chat/stream` | POST | 对话主入口（SSE，事件见 §4.4）。请求体：message 必填 + history_filename/topic/conception/language/field_profile/attachments/regenerate |
| `/chat/history` | GET | 会话列表（mtime 倒序，仅当前账号） |
| `/chat/history/{filename}` | GET/PATCH/DELETE | 详情（basename 收敛）/ 重命名（只改文件内 title）/ 删除（连带删会话向量）；跨账号一律表现为 404 |
| `/chat/upload` | POST | multipart 上传（≤20MB/文件）：统一走 `tools/ingest/attachments.py`，支持 PDF/DOCX/TEX/TXT/MD/Markdown/BIB/PNG/JPG/JPEG/WebP。保存原件 `data/uploads/<uuid>.<ext>` 与文本 sidecar `<uuid>.txt`；上传阶段只做确定性快速提取，**绝不调用 VLM**。扫描 PDF 与图片允许零文本并标记待按需理解。旧版 `.doc` 不支持，明确提示另存为 `.docx` |
| `/chat/file/{file_id}` | GET | 取回提取文本（file_id 正则校验，>200k 字符截断） |
| `/chat/file/{file_id}/raw` | GET | 当前用户上传图片的鉴权预览端点；file_id 仅接受 UUID，且只以内联方式返回 PNG/JPG/JPEG/WebP 原件，不开放任意文件读取 |
| `/health` | GET | 健康检查（含 api_configured） |

**流式取消**：客户端断开（前端 AbortController）时取消后台任务，且不保存当轮历史（干净回滚）。

### 3.2 OpenAI 兼容端点（`/v1`）

见 §7。

### 3.3 产物下载（`/files`）

`GET /files/{filename}`：下载 `data/exports/` 下生成的 `.md/.txt/.docx/.tex` 产物。端点执行 basename 与后缀白名单校验、`is_file()` 检查，并由 `FileResponse` 设置对应 MIME 与 RFC 兼容的 `Content-Disposition`；长中文 DOCX 文件名可直接下载。供 `x_soda.attachments` 与前端文件卡片使用。

### 3.4 账号鉴权端点（`/api/v1/auth`，`backend/app/api/v1/auth.py` + `core/user_store.py` + `core/auth_settings_store.py`）

仅服务自有前端渠道；清小搭渠道（`/v1`）走独立 Agent API Key，与浏览器登录令牌无关。四个认证开关（`auth_required` / `registration_open` / `guest_access` / `email_requirement`）以 `data/users.db` 中的单行运行时设置表 `web_auth_settings`（乐观锁 `version`，进程内 5s TTL 读缓存、写入即刷新）为唯一事实来源：`.env` 中的 `AUTH_REQUIRED` / `REGISTRATION_OPEN` / `GUEST_ACCESS` / `EMAIL_REQUIREMENT` 只在首次读取时 seed 初始行，之后改 `.env` 不影响线上值。管理员在 `/admin/auth-settings` 页面（见 3.5）运行时修改，保存后立即生效。`auth_required=false`（本地模式）时 `/auth/config` 报告本地模式，行为同旧版。开启后：Bearer 令牌解析账号；`guest_access=true` 才允许持浏览器随机 `X-Guest-Id` 的 `guest:<id>` 隔离身份；`guest_access=false` 时无账号令牌一律 401，前端跳转 `/login`。误关账号登录后用 `scripts/enable_auth_required.py` 恢复（写库后约 5 秒生效，无需重启）。开启多用户前可用 `scripts/reown_local_history.py <username>` 将旧 `local` 历史重新归属。

**注册邮箱要求**（`email_requirement` 三态）：`none` = 仅用户名密码（默认）；`collect` = 必填邮箱但不验证；`verify` = 必填邮箱 + 6 位数字验证码（仅此模式可用 `POST /auth/email/send-code`）。验证码经 `core/email_sender.py`（纯标准库 smtplib，SSL/STARTTLS，凭据只读 `.env` 的 `SMTP_*`，绝不入库/入日志）发送，库中 `email_codes` 表只存 SHA-256：10 分钟有效、60 秒重发冷却（429）、最多 5 次尝试、成功或耗尽即删除；`verify` 模式需 SMTP 已配置（PUT 守卫 422）。邮箱统一小写归一，非空邮箱在 `users` 表上有部分唯一索引（`WHERE email <> ''`，存量重复时静默跳过），同一邮箱只能绑定一个账号，注册即 `email_verified=1`；`collect` 存 `email_verified=0`。管理员账号完全豁免：bootstrap 可空邮箱、无需验证。

`users` 表向后兼容迁移 `email/role/disabled/email_verified` 字段。`scripts/bootstrap_administrator.py` 以隐藏输入幂等创建 administrator（邮箱默认留空），绝不重置已有管理员密码或自动提升同名普通账号。`POST /auth/change-password` 校验当前密码并撤销该用户全部浏览器令牌。`agent_api_keys` 表只保存长期密钥 SHA-256、前后缀和使用/撤销元数据；完整 `pa_live_...` 仅创建响应显示一次。管理员 API 为 `GET/POST /admin/agent-keys` 和 `DELETE /admin/agent-keys/{id}`，普通用户 403。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/auth/config` | GET | 公开：`{auth_required, registration_open, guest_access, email_requirement}`，前端据此决定登录页形态与注册字段 |
| `/auth/register` | POST | 注册（201）；`registration_open=false` 时 403；邮箱模式见上（缺邮箱/验证码 422，码错/过期/邮箱被绑定 400） |
| `/auth/email/send-code` | POST | 发送注册验证码（仅 verify 模式且开放注册）；冷却中 429，发信失败 502 且不留码 |
| `/auth/login` | POST | 登录 → `{token, user}`；错误口令 401；用户不存在也跑同等 KDF（防用户名探测时序） |
| `/auth/me` | GET | 令牌 → 用户信息 |
| `/auth/logout` | POST | 吊销令牌 |

### 3.5 管理员访问控制端点（`/api/v1/admin/auth-settings`）

`GET` 返回设置行 + SMTP 非机密状态（`{configured, sender, from_name}`）；`PUT`（strict body + `expected_version` 乐观锁，冲突 409）修改四个开关，守卫：`email_requirement=verify` 且 SMTP 未配置 → 422；把 `auth_required` 关为 false 必须携带 `confirm_disable_auth=true`（防误关——关闭后所有未登录访问立即变为 local 用户，穿透数据隔离，且管理页随之不可用）→ 否则 422。`POST /auth-settings/test-email` 向指定邮箱发测试邮件验证 SMTP 连通性。前端页面 `/admin/auth-settings`（访问控制）：开关行 = 标签 + 问号帮助 + 共享 `AdminToggle`，说明文字全部收进 HelpModal；保存走 draft/diff/sticky 保存栏；关账号登录需在 ConfirmModal 中输入「确认」，关游客访问有危险确认弹窗；本地模式下游客/注册/邮箱开关禁用并显示横幅。

- **口令**：PBKDF2-HMAC-SHA256 60 万次迭代 + 16B 盐（OWASP 基线，stdlib 零新依赖），格式 `pbkdf2$iter$salt$hash`，常量时间比较。
- **令牌**：32B 随机不透明串，库中只存 SHA-256（库泄露不等于令牌泄露）；30 天过期；SQLite `data/users.db`（WAL，全参数化）。
- **隔离**：历史记录写入 `user_id`；list/load/rename/delete 全部按属主过滤（跨账号 = not-found）；同名不同主的记录永不被覆盖（分叉新文件）；同秒同 slug 文件名自动加短哈希防碰撞。无 `user_id` 的旧记录归 `local`。
- **边界说明**：上传文件以 uuid 命名（不可猜测即边界）；/v1 渠道会话无账号概念（清小搭渠道不做个人数据持久化）。

---

## 4. 对话编排层（`agents/orchestrator.py`）

唯一编排路径：原生 function-calling ReAct 循环，`max_iterations=8`，LLM 为 `get_llm("light")` + `bind_tools(get_chat_tools())`。

```
用户消息 (+附件, regenerate 标记)
  → 附件登记 + 入会话 RAG 索引（best-effort）
  → 组装上下文：SystemMessage(系统 prompt)
      + 压缩后的历史（>10 条时旧消息 LLM 压成摘要注入）
      + HumanMessage(用户消息 + [Session Context] + <uploaded_files>)
      + SystemMessage(尾部红线，recency 钉住)
  → ReAct 循环：provider 原生 reasoning_content 立即进入 thinking 通道；
    显式 <thinking>...</thinking> 兼容内容同样逐段流出；普通文字先短暂保留，
    若随后出现 tool_call 则作为工具前说明进入 thinking，否则作为正式 answer 流出
    → tool_call？→ 参数校验(pydantic schema) → 熔断 guard → 重复防护
    → 并行执行（一轮最多 4 个 tool_call 全部执行）→ 规则反射器
    → 原生 FC 反馈（AIMessage.tool_calls + ToolMessage，含 reasoning 回传）→ 下一轮
    → 无 tool_call：流式 answer → done（含 usage）；空补全（无内容无调用）重试 2 次
```

- **上下文窗口**：会话内完整历史在 `session.messages`；发给 LLM 前超窗压缩（保留最近 8 条 + 摘要 SystemMessage）。跨会话无任何记忆。`Session Context` 会持续携带核心集 + 候选集的稳定 `paper_id` 目录，因此用户点名某篇（含候选集 DOI/标题）时模型必须直接调 deep_read/ask_papers，严禁再次 search_papers“定位”。
- **思考双通道**：provider-native `reasoning_content` 会实时写入浏览器/OpenAI SSE 的 thinking/reasoning 通道，并累计到 `done.thinking` 与会话历史；显式 `<thinking>...</thinking>` 作为兼容格式走同一通道。普通文本使用“短暂保留后分类”：检测到同轮 tool call 时作为工具前说明进入 thinking，无 tool call 时才进入正式 answer。provider 要求原生 FC 回传时，累计 reasoning 仍随 `AIMessage` 内部回传。流式层不清洗原始思考中自然出现的工具名、skill 名或路由描述；但 `use_skill` 本身仍是内部指令加载，不产生公开的 tool_start、tool_result、工具卡或历史 tool-call 记录。
- **done 事件瘦身**：`_lite_tool_calls` 剥离大字段（literature_review/summaries/graph/papers/candidates/clusters/timeline/answer）；完整 payload 只进 session.messages 与 tool_result 事件。
- **usage**：trace 累计各次 LLM 调用的 token（`stream_usage=True`）；`/v1` 响应优先用真实值，缺失时按 chars/4 估算（提供商流式不返回 usage 时的兜底）。

### 4.1 ChatSession（`agents/session.py`）

字段：messages / topic / conception / language / field_profile / papers（核心集）/ candidates（候选集）/ paper_summaries / map_data / reading_path / literature_review / sub_directions / search_queries / history_summary / attachments / title / history_filename / trace_ids / **session_id**（RAG 隔离键，uuid，随历史 JSON 持久化）。

持久化：`save/load/list/rename/delete_chat_history` 走 `core/history_store`；旧历史兼容（reserve_papers+reference_papers 并入 candidates，graph_data 等旧字段安全忽略）。删除会话时连带删除该 session_id 的全部向量。

ID 解析仍以当前会话为授权边界：网络论文精确 `paper.id` 优先，只有唯一 DOI 才允许 `doi:`/doi.org 前缀与大小写的窄规范化别名；上传附件精确 id 优先，仅允许当前会话内大小写不敏感、精确且唯一的 filename 作为 provider 容错。未知或歧义别名一律返回校验错误。

### 4.2 工具层（schema: `agents/chat_tools.py`，实现: `agents/tools_impl.py`）

14 个原子工具 + 3 个技能层工具：

| 工具 | 参数 | 说明 |
|------|------|------|
| `search_papers` | topic 必填；conception/language 可选 | §5。产物：核心集+候选集+子方向；topic 命中会话已有论文（id/DOI/精确标题）时 fast-fail 并引导直接用 deep_read/ask_papers |
| `deep_read` | paper_ids/attachment_ids/focus 可选 | §6。网络论文与上传 PDF/DOCX/图片走不同 ID 域；附件按需多模态 |
| `ask_papers` | query 必填；paper_id/attachment_id/top_k 可选 | §8.2。会话级 RAG 问答；paper_id 与 attachment_id 互斥 |
| `research_map` | 无 | §9。主题簇+脉络+谱系图数据 |
| `reading_path` | 无 | §9.3。推荐阅读顺序+理由 |
| `write_review` | 无 | §10。文献综述 |
| `check_structure` | attachment 可选 | §4.6。上传草稿结构体检（纯代码零模型消耗） |
| `check_format` | attachment/spec 可选 | §4.7。格式检查（双模式：纯文本 / LaTeX；spec 逐条对照用户格式要求） |
| `export_manuscript` | title/content 必填，format(md/docx/tex) | §4.7。写作产物导出为可下载文件 |
| `integrity_sweep` | paper_ids 可选 | §5.4。可靠性质检（撤稿/勘误/预印本→正式版，纯官方 API 零模型） |
| `bib_import` | attachment 可选 | §5.5。导入 .bib 文献库到候选集（DOI 补全，与 citation_export 互通） |
| `exhibit_index` | paper_ids/attachment_ids 可选 | §6.3。列出论文或上传附件中的图、表、公式；附件按需理解 |
| `explain_element` | element_id 必填；paper_id 可选 | 读取某一图/表/公式的完整结构化与 VLM 解读；验证当前会话论文/附件授权 |
| `field_census` | 无 | §9.4。领域宏观计量（OpenAlex 聚合 + 1 句画像，区别于 research_map） |
| `use_skill` | name 必填 | §4.5。加载指令技能完整工作流（渐进式披露） |
| `citation_export` | format/bibtex+gbt7714、paper_ids 可选 | 可执行技能：参考文献导出（Crossref 补全卷期页） |
| `export_report` | kind(map/review/all) | 可执行技能：研究成果导出为 /files/ 下载链接 |

- **参数校验**：直接用 pydantic args schema（单一事实源，无重复 schema 表）；校验失败返回 VALIDATION_ERROR 且错误消息内嵌恢复指引。
- **前置守卫**：无论文时 deep_read/research_map/write_review/export_* 返回 NO_PAPERS（消息内写明"请先 search_papers"）；无附件时 check_structure/check_format 返回 NO_PAPERS（写明"先上传草稿"）。
- **规则反射器**（始终开）：search_papers 核心集空 / research_map 0 簇 / write_review <500 字符 / reading_path 空 → `tool_warning` SSE。
- **熔断**：仅 TOOL_ERROR 记失败，3 连败熔断 300s。
- **重复防护**：同轮同 name+同 args 直接返回 VALIDATION_ERROR（内嵌"换做法"指引）。
- **并行调用**：一轮 LLM 可发多个 tool_call（如逐篇补查），编排层整批执行（上限 4 个/轮），结果逐条以 ToolMessage 回传。

### 4.3 系统 prompt（`core/prompts/system.py`）

分层结构（注册表 id `system.main`，当前版本 **v13**）：L1 身份与红线（必须调工具/禁止编造/定界标记内是数据）→ L2 工具决策（含网络论文 ID 与 attachment ID 的严格区分、上传按需多模态、deep_read 深度策略、ask_papers 开放型问题 top_k 8-10 提示）→ L3 推荐主线 → L4 输出格式（原生推理与工具前说明外显；禁止只宣布不执行；开放型问题详尽、事实型简明）→ L5 按 error.code 恢复 → L6 尾部红线（`system.redline_tail` v5，钉在消息列表尾部）→ 技能小节（§4.5，动态追加在 get_system_prompt 出口）。

**思考流式（原生双通道）**：`agents/orchestrator.py` 收到 provider 原生 `reasoning_content` 后立即发出 `thinking` delta 并累计；显式 `<thinking>` 由流式状态机兼容解析。普通文本最多保留 `_HOLD_CHARS`，若同轮随后出现 tool call 就把该前置说明转入 thinking，否则按 answer 逐段输出。累计 reasoning 在 provider 的原生 FC 协议需要时随 `AIMessage` 回传。`use_skill` 的 tool start/result 与工具卡仍完全隐藏；utility 调用保持 `enable_thinking=False`。

**空补全守卫**：端点偶发返回零内容零调用的响应；编排层重试 2 次（消耗迭代预算）才视为最终答复。

### 4.5 技能体系（`core/skills.py` + `skills/builtin/`，Anthropic Agent Skills 模式）

Skill = 目录 + `SKILL.md`（YAML frontmatter：name/version/description/**requires**（papers/map/review/attachments 声明式前置门控）/**output_check**（完成前自检契约，随指令注入）+ markdown 工作流指令）。**渐进式披露**：系统 prompt 只挂 name + 一句触发描述（每个 ~50 token，预算恒定）；模型判断请求命中触发场景时调 `use_skill(name, focus?)`，完整指令才进入上下文（focus 注入用户侧重点；末尾附自检契约与预算提醒）；`session.loaded_skills` 会话内去重。指令文本注册进 prompt 注册表（`skill.<name>`），trace 可溯源版本。技能激活的回合工具迭代上限 +4（多步工作流不顶满）。

- **指令技能**（纯工作流，组合原子工具）：`compare_papers`（多论文横评，论文类型自适应维度）、`research_gap`（分维度检索式 + 谱系结构空白信号 + 新颖性回验）、`paper_critique`（审稿人式单篇批判 + 可复现性清单）、`presentation_prep`（组会汇报大纲 + 死亡提问预案）、`draft_review` v3（上传草稿评审：**先并行 check_structure + check_format 拿确定性事实**再逐节定性评审 + 相关工作漏引对照，前置 attachments）、`related_work`（相关工作章节写作，按流派组织）、`paper_writing` v2（写作教练四模式：逐章框架/初稿起草（数据一律占位符，诚信红线）/段落批改/语言润色，无前置要求）、`topic_advisor`（选题方向指导：聚焦→新颖性取证→可行性→2-3 个带文献锚点的方向变体）、`structure_advisor`（按论文类型推荐章节骨架 + 篇幅比例 + 章间逻辑）、`format_compliance`（按用户给定格式要求逐条对照，排版项诚实转人工核对清单，前置 attachments）、`evidence_anchor`（任意草稿段落的逐句证据核对：拆出可证伪主张→逐条 ask_papers 取证→"原文句→主张→证据与强度→建议插入位置"对照表，前置 papers）。写作主链：topic_advisor → structure_advisor → paper_writing(初稿) → check_structure/check_format → draft_review → evidence_anchor（段落证据）→ integrity_sweep（投前质检）→ export_manuscript。
- **可执行技能**（确定性代码，独立工具）：`citation_export`（BibTeX / GB/T 7714 双格式，Crossref 按 DOI 补全卷期页，默认核心集）、`export_report`。
- **技能递名片（skill surfacing）**：工具结果里数据驱动地追加"[技能提示]"（search_papers→compare/gap/citation；deep_read→critique/prep；……注册表校验，已加载不重复提示），对抗系统 prompt 元数据随对话滚动的遗忘。
- **调度准确性靠 description**（写清何时触发/不触发）+ dispatch 黄金集回归（`tests/eval/dispatch_golden.yaml`，21 条话语 → 期望技能/none，在线跑 `run_dispatch_eval.py`，离线 scorer 单测）。
- **扩展方式**：往 `skills/builtin/` 加一个目录 + SKILL.md 即注册，无需改代码；malformed 文件告警跳过；旧字段 `requires_papers: true` 自动映射为 `requires: [papers]`。

### 4.6 结构体检（`tools/writing/structure_check.py`，确定性工具）

与谱系图同一原则：**事实由代码算，建议由模型给**。`check_structure` 对用户上传草稿的提取文本做纯 Python 解析（零 LLM 消耗）：章节树（markdown/数字编号/中文「一、」标题 + PDF 提取常见的裸行标题）、IMRaD 缺失章节（中英文模式，按严重度分级）、章节字符占比失衡（阈值标注为参考值）、摘要长度、引用卫生（编号 `[n]` 区间展开 + 作者-年份计数 vs 参考文献条目，交叉出"疑似未引用条目"）、图表引用统计。产出 ToolResult：结构化 facts + markdown 体检报告（前端专用卡片渲染），Agent 据此做定性解读并接 `draft_review` 技能完成逐节评审。

### 4.7 格式检查与文稿导出（`tools/writing/format_check.py` + `manuscript_export.py`）

`check_format` 同为零 LLM 确定性工具，双模式：纯文本模式（图表编号连续性/正文引用、引用风格混用、GB/T 7714 类型标识命中率、关键词数量、多级标题断号）与 LaTeX 模式（自动识别，查 `\cite` 无参考文献块、`\ref` 无 `\label`、abstract 环境）。`spec` 参数接收用户粘贴的格式要求原文，关键词规则引擎映射可文本化检查项（摘要字数/关键词个数/正文规模/GB/T 7714）逐条对照；字体/行距/页边距等排版项**诚实降级**为"需 Word/LaTeX 人工核对"清单。`export_manuscript` 把写作产物（初稿/润色稿/修改清单）的 markdown 转为 docx（python-docx：标题/列表/加粗/表格）、tex（ctexart 中文可编译）或 md，写入 `data/exports/`。`GET /files/{filename}` 仅接受 basename 及 `.md/.txt/.docx/.tex` 白名单，使用 `FileResponse` 返回正确 MIME 和 RFC 5987 兼容的 UTF-8 `Content-Disposition`，因此长中文 DOCX 文件名也可直接下载；清小搭渠道经 x_soda 附件通道下发（§7.4）。

### 4.4 对话 SSE 事件

`step` / `thinking`(delta) / `answer`(delta) / `tool_start` / `tool_result` / `tool_warning` / `tool_progress` / `heartbeat`(15s) / `history_saved` / `done`(thinking/answer/tool_calls 瘦身/trace_id/usage) / `error`。

---

## 5. 搜索子系统（`agents/search_agent.py` + `tools/search/`）

```
topic (+conception)
  → ① 意图理解（1 次 LLM）：还原缩写/短主题 → research_goal
      + 2-4 个子方向 × 中英检索式；JSON 容错解析，失败回退 [topic]
  → ② SearchManager.search_all（启用源 × 查询并发，120s 全局 deadline，去重）
  → ③ 语义重排 rerank_papers：
      score = 0.55*cos(本地 MiniLM) + 0.20*log被引 + 0.15*时效 + 0.10*token重叠
      嵌入不可用时 0.65*token重叠 + 0.20*log被引 + 0.15*时效（降级链）
  → ④ 自适应分层 adaptive_tier：
      候选集 = score ≥ max(0.35, top1*0.45)，封顶 25 篇
      核心集 = 候选集头部，在 [5,12] 内找最大相对分数断崖（elbow≥0.25）截断
  → ④.5 全文可获取性探测（`tools/pdf/availability.py`）：
       对核心集 + 候选集逐篇解析 OA 候选 URL（源 API pdf_url + Unpaywall）
       → 只发 Range 请求读取前几 KB 检查 `%PDF-` 文件头（不下载全文）
       → 写出 fulltext_status: available / unavailable / unknown
       付费墙落地页（HTML）判定为 unavailable；超时/网络抖动保持 unknown
  → ⑤ 写 SQLite 缓存 + fulltext_status 缓存 + 会话向量库 L1（title+abstract）
```

- 无 X/Y/Z 参数、无 Flash/Pro 模式——分层是确定性算法，不花 LLM。
- **数据源与限流**（`tools/search/base.py::RateLimiter`）：OpenAlex 5/0.5s、arXiv 1/3.0s（ToU 硬性规定：单连接、3 秒 1 请求）、Crossref 5/0.5s、Europe PMC 3/0.5s、DOAJ 2/1.0s、HAL 3/0.5s、OpenAIRE 3/0.5s、CORE 3/0.5s（无 CORE_API_KEY 自动跳过）、Semantic Scholar 3/1.5s（无 key 低频可用，配 S2_API_KEY 更稳）；429 读 Retry-After 精确等待或指数退避 ≤3 次；`trust_env=False` 直连。全部九个源均为免费合法官方 API；全文只跟随各源声明的 OA 链接（另见 §6.2 的 Unpaywall 与 OA-only 策略）。**邮箱参数策略**：OpenAlex/Crossref 未配真实邮箱时匿名访问（绝不发占位身份）；Unpaywall 强制邮箱，未配置则完全不调用。逐平台许可与合规细节见根目录 [Official_Paper_Platform_License_Description.md](../Official_Paper_Platform_License_Description.md)。
- **去重**：DOI 精确 → 标题归一化精确/模糊（Jaccard≥0.95）→ 字段合并（摘要取长、被引取大、来源取并集）。
- 搜索结果注入 LLM 上下文时用 `<search_results>` 定界标记（数据非指令）。
- **全文状态不算命**：`paper.pdf_url` 只表示"源 API 声称有链接"（经常是 ACM/Springer 付费墙落地页），绝不直接当作全文可获取。`tools/pdf/availability.py` 在检索完成前解析 OA 候选（源 API + Unpaywall）并**轻量探测**：只发 Range 请求读取前几 KB，确认响应头与 `%PDF-` 文件头，**不下载全文、不跑结构解析**；写入 `fulltext_status`（available/unavailable/unknown）到 SQLite `fulltext_status` 表（30 天 TTL，缓存命中免重复探测）。真正的全文下载与解析仍只在 deep_read / ask_papers 按需升级时发生，其结果回写同一状态字段。`research_map` 谱系图节点和前端检索卡片只显示这一已验证状态；unknown 显示"待验证"。

### 5.4 可靠性质检（`tools/search/integrity.py`，确定性 · 零 LLM）

`integrity_sweep(paper_ids?)` 是"写综述前/投稿前必跑"的质检闸。逐篇（并发 + 既有限流器）：
- 有 DOI → OpenAlex `GET /works/doi:{doi}?select=is_retracted,is_paratext,merged_into` 读撤稿标志（复用 `openalex_refs` 单条 DOI 查询模式 + `_mailto`）；Crossref `GET /works/{doi}` 读 `relation`（`has-retraction`/`has-correction`/`has-expression-of-concern`，表示本文是被处置方）+ `assertion`（复用 `export/enrich` 请求模式）。
- arXiv 篇（`source=="arxiv"`）→ 有 DOI 即提示"已有正式版"；无 DOI 的取前 5 篇查 arXiv `journal_ref`（受 3s/请求 ToU 约束，封顶避免拖慢）。
- 分级：✅ 无异常 / ⚠️ 勘误或关切 / ⛔ 撤稿（附通知来源）/ 🔁 预印本已正式发表 / ❓ 未查到（无 DOI 或查询失败，诚实降级，不算错误）。复用 `RateLimiter` 与匿名邮箱策略。

### 5.5 文献库导入（`tools/export/bibtex_import.py`，确定性 · 零 LLM）

`bib_import(attachment?)` 与 `citation_export` 构成双向互通。上传通道放行 `.bib`（`ingest.extract_text` 文本透传，本质是文本，无需新下载器）。纯 Python 容错解析器（`@type{key, field={...}/"..."}`，嵌套花括号/引号，不引新依赖）逐条提取 title/author(按 `and` 切)/year/doi/venue/abstract；有 DOI 的经 `enrich_by_dois` 补全；与会话既有论文去重（`generate_paper_id` ID/DOI 相等 → `title_similarity≥0.95` 标题模糊）后并入 `candidates`（不污染核心集）并写会话向量索引 L1。输出导入报告（成功/补全/跳过及原因）。

---

## 6. 深读子系统（`agents/reader_agent.py` + `tools/pdf/`）

### 6.1 深度策略（`core/reading_policy.py`：deep_read 默认全文 + ask_papers 证据缺口）

- **deep_read 是显式全文操作**：用户调 deep_read 后，系统对所选网络论文**逐篇尝试下载合法 OA 全文并做全文级深读**，不再按 focus 把多数论文留在摘要级。只有取不到 OA 全文（付费墙/无 OA 副本/解析失败）的论文才回退摘要级。
- **报告与实际层级严格一致**：工具结果按「已取得 OA 全文并完成全文级深读」和「未取得全文、按摘要级处理」分别计数，并列出未取得全文的论文标题与原因。摘要永远不会被写成 `full_text`，因此不会出现“工具说已读全文、后续 RAG 却取不到全文”的错位。
- **per-paper 全文状态全程同步**：`Paper.fulltext_status` 三态（available/unavailable/unknown）由检索期验证、deep_read 实际结果和 ask_papers 按需升级共同维护，并回写 SQLite `fulltext_status`。研究地图谱系图与检索卡片只消费该字段，不再看 `pdf_url`。
- **缓存自愈**：`summary_cache` 的 `full` 行只有真正携带全文才有效；旧版本把摘要回退误存进 `full` 键的行会在下一次读取时被丢弃并重新尝试，不需要人工清库。
- **ask_papers 证据缺口升级不变**：答案检出证据缺口（"未报告"占位 ≥2 处；或数据型问题命中段落全部来自摘要区段）时，同一调用内自动补读 top ≤2 篇全文 → 重检索 → 重生成（一次为限）。升级预算只约束这次自动补读；显式 deep_read 不受 ≤3 篇/会话软上限限制。
- **ask_papers 针对单篇深问（paper_id）**：该篇无全文时自动尝试下载解析 → 切块入会话 RAG；取不到全文时工具摘要显式写明「本次回答仅基于摘要级证据」。
- **用户上传 PDF/DOCX/图片**：上传时只做快速文本提取；深读或图表问题出现时再按需运行结构/OCR/VLM，并复用指纹与图像哈希缓存。

硬预算：ask_papers 单次升级 ≤2 篇、每会话自动升级软上限 8 篇（`session.full_read_count` 只统计真正取得全文的次数）；SQLite 摘要缓存 + 会话向量库使重读零成本；拿不到 OA 全文属正常态，回退摘要级并明确证据边界。

### 6.2 网络论文 PDF → RAG 多模态管线

`agents/reader_agent.py::parse_and_understand` 是 `deep_read` 与 `ask_papers` 按需全文升级（`_ensure_fulltext`）共用的唯一 chokepoint；两条路径严格复用同一份理解缓存，不存在重复视觉消费。

1. **结构解析**（`tools/pdf/structure/`）：`get_structure_parser()` 默认使用 Docling（布局、OCR、表格结构），失败自动回退 PyMuPDF。输出 `ParsedPaperDocument`：带页码章节、图/表/公式 `PaperElement` 清单、bbox、PNG 裁图路径、`image_hash`、原始文本、扫描标记与 PDF SHA-256 `doc_fingerprint`。旧 `tools/pdf/parser.py` 不再存在。
2. **扫描页恢复**（`core/multimodal/ocr.py`）：`is_scanned` 时对结构层未恢复的前几页做 VLM-OCR；视觉服务不可用或失败时保留现有文本，不再因 `no_text` 硬失败。
3. **元素语义理解**（`core/multimodal/analyzers.py`）：有界并发、逐元素容错，按图 > 表 > 公式排序，并受 `ELEMENTS_PER_PAPER_CAP` / `VISION_CALLS_PER_PAPER` 硬预算约束。图像哈希缓存命中免费且不占调用预算；表格优先保留 Docling TableFormer markdown，公式允许 VLM LaTeX 修正结构层提取。
4. **全局持久化**：SQLite `paper_elements` 以 `doc_fingerprint` 判断新旧；匹配时直接恢复已理解元素，并补建可能丢失的全局 `elements` Chroma 索引，不重新 parse 后续语义、不调 VLM、不重写数据库。轻量元素引用进入摘要。
5. **会话文本索引**：摘要进入 L1，全文按章节父子切块进入 L2，均带 `session_id`。元素向量是全局缓存，但检索时只允许当前会话的论文 ID 集合。

在线 PDF 下载仍坚持 OA-only：候选为源 API 的 OA PDF 与 Unpaywall `best_oa_location`；scheme/DNS/公网 IP/云元数据/50MB/魔数/文件名全部校验，付费墙链接不跟随。拿不到 OA 全文属于正常态，回退摘要级并明确证据边界。**下载落盘策略**：web 渠道保存在 `data/pdfs/<paper_id>.pdf`，无计划清理、跨会话复用；API 渠道 `public_pdf` 按内容寻址共享并在 3 天 TTL 到期后由计划清理删除，后续 deep_read 会自动重新下载并提取。临时下载文件只存在于下载过程，失败/中断在 `finally` 中清理，成功原子替换为持久文件。字段模板仍为 cs/social_science/medical/humanities/general 五套。

### 6.3 上传附件的按需多模态

上传附件是独立数据域，不伪装成论文：不会进入 `session.papers`、`session.candidates` 或 `paper_summaries`。文本 chunk 使用原始 `<attachment_id>`；元素文档使用 `upload:<attachment_id>` 命名空间。

- **PDF**：原件按需交给同一个 `parse_and_understand`，因此与网络论文拥有完全一致的布局、OCR、元素理解与缓存语义。
- **DOCX**：上传时提取段落和表格 markdown；按需阶段从 `word/media/*` 提取内嵌图片为 figure 元素并进行视觉理解。
- **PNG/JPG/JPEG/WebP**：按单个 figure 元素处理，保留真实 MIME 发送给 VLM。
- **纯文本格式**：TEX/TXT/MD/BIB 直接使用 sidecar，不调用视觉模型。
- **缓存与降级**：上传同样复用 `paper_elements`、`vision_cache` 和全局 elements 向量；理解后的元素文本写回 sidecar 并重建当前会话文本索引。无 VLM、熔断或单元素失败时降级为文本/caption/结构提取，不阻塞读取。

全局缓存不等于全局授权：检索范围始终是当前会话网络论文 ID + 当前会话附件的 `upload:<id>`；`explain_element` 也先验证该论文或附件确属当前会话，不能仅凭 SQLite 中存在记录跨会话读取。

### 6.4 图表导览与元素解读

`exhibit_index(paper_ids?, attachment_ids?)` 对网络论文优先返回已持久化的图/表/公式元素；必要时仍可从 PDF/full_text 提取 caption。对上传 PDF/DOCX/图片会先按需理解再列出元素。`explain_element(element_id, paper_id?)` 读取 SQLite 中某一元素的完整 `understanding`、结构提取、页码、章节、caption 与受限图片 URL。前端点击元素可继续追问；视觉不可用时仍返回可获得的结构/caption 信息。

---

## 7. OpenAI 兼容层（`/v1`，清小搭接入）

| 端点 | 说明 |
|------|------|
| `GET /v1/models` | 连通性 + 凭证校验，返回 `paper-agent` |
| `POST /v1/chat/completions` | 内部跑 chat_turn；stream=false 返回标准 JSON；stream=true 真流式 SSE |

### 7.1 真流式帧序

role 帧 → `delta.reasoning`（provider 原生 reasoning、显式 `<thinking>`、工具调用前说明、带参 emoji 进度文案与 15s 心跳）→ `delta.content`（**Markdown 卡片块** 与 answer 增量，见 §7.6）→ stop 帧（finish_reason=stop/length + usage + x_soda）→ `data: [DONE]`。帧随 agent 事件即时转发（非跑完重放），流式层不改写 provider 原始 reasoning；`use_skill` 的公开工具开始/结果事件仍被忽略，但技能**新加载**成功会触发专用 `skill_loaded` 内部事件（只携带技能名），`/v1` 通道将其同时映射为思考折叠提示（`📘 已加载技能《标题》，按其工作流执行…`）与正文技能行（`━━ 📘 技能 · 标题 ━━`，可经展示策略关闭），web 通道原样转发该命名事件而前端忽略——自制前端行为不变。未产出内容即失败 → HTTP 5xx；流式中途出错 → stop 帧 + error 字段 + [DONE]。

**alias 一致性不变量**：卡片块与技能行作为 `delta.content` 的一部分进入 `content_parts` 与 `finalize_turn` 的 `final_answer`，保证下一轮 `canonical_message_chain` 与清小搭回传的 assistant 内容逐字一致——否则跨轮会话续接会断裂。卡片与回答共享 `max_tokens*4` 内容预算，且卡片占比被硬性限制在 60% 以内；预算过小（<2000 字符）时直接跳过卡片。

### 7.2 API 独立存储、会话身份与 7 天 Checkpoint

`/v1` 不再依赖首句哈希的 2 小时内存 SessionMemory。鉴权先得到不含明文 Key 的 principal（数据库 `key_id/created_by/source`；应急/开发 token 只使用单向摘要），再对 `credential_id + optional OpenAI user + canonical prior messages` 做服务器 HMAC。当前最后一条 user 消息不参与 lookup；正常响应结束后，以请求完整消息链加实际 assistant 内容建立下一轮 alias。相同开场的首轮不会共享；一个 alias 指向多个 session 时标记 ambiguous，后续拒绝猜测并新建会话。

API 工作状态写入 `data/openai_api/state.db`：版本化 JSON + zlib，未压缩总量上限 8 MiB，单字段超过 256 KiB 外置为私有 `state_payload` artifact。只保存 topic/profile、论文候选、结构化摘要、地图/路径/综述、附件 artifact 引用、loaded skills、深读计数和 RAG session id；**不保存完整 messages、reasoning/思维链、API Key、raw bytes、完整 PDF 正文或默认 Trace**。TTL 默认 7 天，SQLite optimistic version + 单进程 asyncio lock 防止陈旧 writer 覆盖。重启后可恢复研究状态和 API 专属 RAG；请求内 messages 仅在 miss 时临时补齐文本上下文。

API 物理根目录固定隔离为 `data/openai_api/{state.db,metadata.db,chroma,blobs,tmp,traces}`。web 继续使用原 `history_record`、`data/uploads`、`data/pdfs`、`data/assets`、`data/exports` 和 web Chroma；API 清理器没有权限扫描这些路径。

### 7.3 多模态输入（`backend/app/api/v1/multimodal.py`）

content 数组解析：`text` 直取；`file` 与 `image_url` 统一经过 `save_attachment`，支持 PDF/DOCX/TEX/TXT/MD/BIB/PNG/JPG/JPEG/WebP。HTTP(S) URL 逐 redirect 做 SSRF/公网地址校验并流式写入 0600 temp，边写边执行 50 MiB 上限，成功原子转为当前 session 的 HMAC 内容寻址私有 artifact，失败/中断删 temp；也支持 `data:` URI。只把附件引用登记到当前 `/v1` Checkpoint 并建立 API 专属文本索引，**摄取阶段零 VLM**。公共 OA PDF 按 SHA-256 跨 API session 复用，私有上传绝不跨 session 自动共享。后续 `deep_read`、附件问答或图表请求才按需理解并复用缓存。`input_audio` 仍明确不支持并要求文字转写。

### 7.4 文件产物输出（`x_soda.attachments`）

本轮成功调用 research_map/write_review 时，`tools/export/report.py` 写入 API 独立 export artifact；research_map 同时产出 Markdown 结构化报告和静态 SVG 谱系图（标题块/彩色簇泳道标签/被引三档节点/奠基光环/`+N` 聚合桶/年份网格/图例，与前端 `GenealogyGraph` 同布局规则），write_review 产出 Markdown。非流式挂响应顶层、流式挂 stop 帧：`{fileUrl, fileName, fileType, mimeType, fileSize}`（4 必填+size，image 类自动补可选 `previewUrl`）。展示名与物理 hash 路径分离，每次导出产生唯一 public alias；fileUrl 由请求 base URL 拼 `/files/{alias}`，清小搭负责转存。`/files` 先解析未过期 API alias，再回退 web exports；过期 API alias 固定 404。此外，`data.files` 型工具结果（export_report / export_manuscript）在当轮同样转换为附件下发——写作产物导出在清小搭侧也是可下载文件卡片。

三类工具结果还会追加**当轮即时附件**（`openai_compat._extra_attachments`，均走同一 export artifact 管道与 24h TTL）：`explain_element` 的图/表裁剪图 PNG（落盘位置按 `settings.reader.assets_dir` 解析，且重新校验该文档属于当前会话的元素 scope，会话隔离红线在附件层二次生效）；`citation_export` 的 `.bib`/`.txt` 引用文件；`field_census` 的纯 SVG 趋势图（年度折线 + 高产作者/机构横条，`tools/export/cards.py::render_field_census_svg`）。

协议边界必须明确：`openai-compatible-agent-integration-guide.md` 只定义正文/推理增量和 `x_soda.attachments`，没有任意 React 工具卡或交互图谱组件协议。因此自有前端的 `SearchResultCard`、`ResearchMapCard`、`GenealogyGraph` 等不能原样出现在清小搭；清小搭接收 Agent 的自然语言总结、Markdown 卡片仿真（§7.6）、附件文件卡和静态 SVG 图。筛选、缩放、节点详情及“深问这篇”等交互继续由本项目 `/chat` 提供，不发送私有未声明字段冒充兼容能力。

### 7.5 生命周期、磁盘压力与 API Trace

默认保留：session/upload 7 天、export 24 小时、public PDF 3 天（过期即清理，后续 deep_read 自动重新下载提取）、语义/视觉缓存 90 天、Trace off；hourly cleanup 只在 API 根目录内执行过期、孤立对账和压力清理。75% 清过期并告警；85% 连续清理公共/生成/向量/视觉等可重建数据，不提前删除未过期私有上传；95% 默认 `pause_heavy`（上传、下载、deep_read、OCR、VLM、导出暂停，文字聊天继续），管理员可经预览和二次确认改为 `emergency_evict`；98% 强制暂停文件重任务，不可关闭。in-flight、`protected_until` 和一小时内未完成登记文件受保护。

- **账号数据清理**（`/admin/accounts-data` + `core/admin_accounts.py`）：管理员统一查看 web 账号与 Agent API Key 的数据占用（history_record / data/uploads / trace / API session+private blob），按账号彻底删除。文件删除前全量覆写 + fsync，API state.db 使用 `PRAGMA secure_delete` 并 `VACUUM` 回收空间；共享 PDF/assets 缓存单独一键清理，deep_read 需要时自动重下。

API Trace 与 web Trace 独立：off 只写匿名请求/错误/Token/耗时聚合；metadata 仅 trace id、模型、工具名、状态、错误码、Token、耗时；full 仅供临时排障并脱敏 Key、bytes、完整正文和 URL query，最长 7 天。web Trace 仍按原路径和原始行为工作。管理员浏览器页面 `/admin/api-storage` 提供策略、容量、状态、清理记录、完整帮助 catalog 和危险操作确认；Agent Key 无管理权限。

### 7.6 Markdown 卡片仿真与展示策略（`/admin/display-policy`）

`tools/export/cards.py` 是 `/v1` 通道专属的卡片渲染层：每个工具结果（`ToolResult.to_dict()` 载荷）可渲染为一段紧凑 Markdown 卡片（emoji 卡头对齐前端 `TOOL_META`、列表优先保证纯文本降级可读、top-N 截断、单卡 1200 字符上限、缺字段优雅降级为一行摘要或空），在工具完成时以 `delta.content` 插入、位于最终回答之前——复刻自制前端"卡片在回答上方"的布局。8 个核心工具（检索/研究地图/阅读路径/深读/元素解读/领域普查/综述/引文导出）有完整卡片渲染器，其余工具渲染一行摘要；`explain_element` 按图/表/公式分三态（VLM 描述 / GFM 表格 / LaTeX 代码块）。

策略存于 `data/openai_api/state.db` 的 `api_display_policy` 单行表（schema v5，乐观锁版本并发）：`preset ∈ {core, all, custom, off}`（默认 core）、`enabled_tools`（custom 时的显式工具集）、`skill_card_enabled`（正文技能行开关；思考折叠提示始终保留）。管理员经 `/api/v1/admin/display-policy`（GET/PUT，复用 `_administrator` 鉴权与 `expected_version` 乐观锁）与前端 `/admin/display-policy` 页面配置；每请求读取一次（存储读取失败时降级为 core 默认，绝不让对话回合失败）。`off` 关闭全部 Markdown 卡片，思考折叠中的进度提示与文件附件不受影响。

`scripts/preview_qxd_cards.py` 在本地把全部卡片与两张 SVG 渲染到 `data/preview/`（gitignored）供部署前目检，不参与部署。

### 7.6 其他契约

- 鉴权：Bearer 支持数据库长期 Agent API Key（只存 SHA-256、可撤销）和迁移/应急 `AGENT_API_KEY`（常量时间比较）。本地无配置时接受任意非空 token；生产从未配置任何 Agent Key 返回 503，错误或已撤销 key 返回 401
- 请求验证：`stream` 使用 `StrictBool`；消息非空且至少有一条 user；角色/content part 白名单；`model` 可缺失、空或 null；`max_tokens` 为严格正整数；畸形 JSON 为 400，schema 错误为 422
- `stream` 严格按 JSON 布尔；`max_tokens` 接受并按 chars/4 软截断（截断时 finish_reason=length）；finish_reason 只用官方 5 值
- usage：trace 真实 token 优先，缺失按 chars/4 估算

---

## 8. 检索层（会话级 RAG）

### 8.1 嵌入、向量库与精排（`core/embeddings.py` + `tools/storage/vectorstore.py` + `tools/retrieval/rerank.py`）

- 本地 `paraphrase-multilingual-MiniLM-L12-v2`（~120MB，CPU，离线，中英双语）；加载/编码失败全链路降级。
- ChromaDB 三 collection（cosine）：`summaries`（L1：title+abstract+findings）、`fulltext_chunks`（L2：父子切块，见下）与全局 `elements`（L3：图/表/公式的 caption + 结构提取 + VLM 理解文本）。
- **结构化父子切块**（`tools/retrieval/chunking.py` + `vectorstore.build_fulltext_chunks`）：段落/句子边界切块（CJK+拉丁句读，绝不句中截断；单句超长的最后手段硬切），重叠以整句携带；索引单元=~500 字符**子块**（检索精度），每个子块 metadata 带 `parent_text`（~1500 字符父段）+ `parent_id`。
- **cross-encoder 精排**：本地 `BAAI/bge-reranker-base`（~1.1GB，CPU，sentence-transformers 加载，无新依赖）；`storage.reranker_model` 置空即关闭；懒加载单例，下载/加载/打分失败回退 RRF 原序。
- **会话隔离**：L1/L2 记录 metadata 带 `session_id`，id 以 `{session_id}::{paper_id}` 命名空间；全部检索强制 `where session_id=当前会话`，删会话连带删向量。上传文本 chunk 的 paper_id 是 `<attachment_id>`。L3 为跨会话复用视觉成本的全局集合，不带 session_id，但每次查询必须显式过滤为“当前会话网络论文 ID + 当前会话 `upload:<attachment_id>`”；因此缓存全局、授权仍是会话级。

### 8.2 ask_papers 检索管线

```
用户问题 → ① 查询改写（1 次 light LLM，prompt 注册表 qa.query_rewrite；失败回退原 query）
        → ② 混合检索：文本向量轨（Chroma 子块，会话过滤）+ BM25 轨（父段粒度语料，CJK bigram）
             + 元素向量轨（当前会话 paper/upload scope；规则检测 figure/table/formula 并可按 kind 收窄）
             各召回 3×top_k，RRF(k=60) 融合取 2×top_k
        → ③ 精排：cross-encoder 对 2N 候选重排取 top_k（无模型时保持 RRF 序）
        → ④ 父子扩展：命中子块按 parent_id 去重，父段文本喂给 LLM（small-to-big）
        → ⑤ LLM 生成（注册表 qa.answer，深度自适应：事实型 2-4 句直答；
             开放/综述型结构化详答，保留方法名/数据/数字，禁止过度概括）
        → ⑥ 证据缺口检测（reading_policy："未报告"占位/摘要-only 命中数据型问题）
             → 命中则自动补读 top≤2 篇全文，重检索重生成一次
        → ⑦ _validate_citations 防幻觉
```

检索用改写后查询，生成用原始问题；模态检测使用原问题，避免改写丢失“图 3/表格/公式”等信号。向量不可用时 BM25 单独兜底。返回 answer + sources（paper/section/snippet），附件问答还返回理解状态供前端同步。**索引自愈**：文本索引缺失时按当前会话论文/附件补建；指纹命中的元素也会补建全局 element 索引而不触发 VLM。指代解析由系统 prompt 约束：网络论文传 paper_id，上传附件传 attachment_id，二者互斥且不可混用。

### 8.3 全文可读核心保障与结构问答

全文可获取性必须在相关性分层后由真实 OA PDF 文件头探测确认。探测结束后，`rebalance_core_for_fulltext` 执行确定性 best-effort 后处理：核心层 verified `available` 数量目标为 `min(5, 核心+候选全部 available 数)`；从候选按 `relevance_score` 降序提升，原核心不替换、核心允许扩容、候选删除提升项且不补位，`unknown/unavailable` 不计。该操作不改变论文总数，也不使用 `pdf_url` 猜测。

`PaperSummary.section_outline` 保存解析器给出的完整有序章节标题与页码范围；`document_info` 保存 `read_level/pdf_fetched/parser_backend/page_count/text_chars/section_count/is_scanned/ocr_status/ocr_chars/element_count/vision_understood_count`。两者是轻量可观测元数据，不含绝对路径、PDF bytes 或第二份完整正文。旧全文缓存缺字段时允许从本地 PDF 结构重解析自愈；元素理解仍由 `doc_fingerprint` 命中 SQLite 缓存，禁止重复 VLM 消耗。

章节结构、目录、逐节说明等查询会把 `section_outline` 作为受保护 passage 放在生成上下文首位，再用剩余槽位承载原有 hybrid RAG 结果。普通方法、实验、数字和图表问题仍使用向量/BM25/元素轨、RRF 与 cross-encoder。

OCR 状态严格区分三层：Docling 的数字文本/内置 OCR、仅扫描件触发的 Stage 1.5 VLM-OCR、图/表/公式的 VLM 语义理解。数字文本 PDF 的 `ocr_status=not_needed` 表示正常成功；扫描件为 `recovered` 或 `unavailable_or_empty`。VLM 未配置、熔断或失败始终降级，不使深读硬失败。


---

## 9. 研究地图与谱系图（`agents/map_agent.py` + `tools/storage/genealogy.py`）

### 9.1 研究地图（research_map，LLM 预算 2 次）

1. **主题聚类**：embed(title+abstract) → 凝聚聚类（cosine，distance_threshold 0.5，单簇且论文>6 时自动收紧到 0.4 重试；嵌入不可用回退按子方向分组）→ 1 次 LLM 为全部簇命名+概述（id 容错匹配"簇0"→0 + 位置兜底）；
2. **领域脉络**：1 次 LLM 读各簇 label+overview（标注失败时注入代表论文标题兜底）输出 3-5 句整体图景；
3. **时间脉络**：按 year 分组的确定性时间线（无 LLM）；
4. **谱系图数据**：见 §9.2。

产物：`{clusters, timeline, landscape, graph:{nodes, edges}}`。

### 9.2 谱系图数据（genealogy.py）

- **真实引用边**：`tools/search/openalex_refs.py`（OpenAlex `referenced_works`，免费无 key），方向 citer→cited；失败降级为空（不阻塞）。
- **语义边**：嵌入余弦 ≥0.5，跳过已被引用边连接的节点对，每节点 ≤3 条。
- **角色**：PageRank top → 奠基；介数中心性 top → 桥梁（无引用边时按被引数兜底）。
- 节点：核心集实心/候选集空心标记 + year/citation_count/cluster/role/url + abstract（前 200 字符）+ venue（详情面板用）。

### 9.3 阅读路径（reading_path，LLM 预算 1 次）

确定性挑选 ≤6 篇：奠基（PageRank）→ 各簇代表（桥梁，簇内最高被引）→ 前沿（最近年份高被引），去重；1 次 LLM 为每篇生成"为什么先读它"（≤40 字，JSON 容错）。

### 9.4 领域普查（`tools/search/openalex_census.py`，确定性聚合 + 1 utility LLM）

`field_census()` 回答"整个领域长什么样"，与 research_map（手里这批论文的关系结构）严格分工。以会话检索式调 OpenAlex `group_by` 聚合（全仓首次使用该 API）：`publication_year`（`from_publication_date:2011-01-01` 过滤近 15 年，升序）+ `authors.id` / `institutions.id` / `primary_location.source.id` 各 top10。复用 `RateLimiter` 与匿名邮箱策略；4 个维度并发、各自独立降级为空。1 次 `ainvoke_utility` 写 ≤3 句领域画像（增长/饱和、谁主导、发在哪），失败回退纯表格。前端渲染纯 SVG 趋势折线（`rgb(var(--token))` 主题自适应，零依赖，沿用 GenealogyGraph 原则）+ 三个 top-N 条形榜。

---

## 10. 文献综述（`agents/review_agent.py`）

结构：引言 → 各主题簇一节（簇来自 research_map，无地图时单一组）→ 跨主题分析 → 结论（gap 来自 limitations/future_work）。防幻觉：注入可用论文列表要求按 `[paper_id]` 引用，输出后 `_validate_citations` 校验（不存在 → 删除或 [CITATION NEEDED]）；最后把幸存引用渲染为 `Title ([Link](url))`。语言由 `language` 显式控制（prompt 后缀）。

---

## 11. 前端（`frontend/`）

- **chat 单页**：`/` 重定向 `/chat`；思考折叠块、按工具类型渲染的结果卡片、文件上传 chip、复制/重新生成/停止。`use_skill` 完全是内部操作，不显示工具卡，也不会从旧历史恢复出来。
- **附件 UI**：选择器与拖拽支持 PDF/DOCX/TEX/TXT/MD/BIB/PNG/JPG/JPEG/WebP。统一 `ChatAttachment` 保存扩展名、MIME、`multimodal_status`、元素数与预览 URL；图片通过带 `authHeaders()` 的 fetch 取 Blob URL 预览并在卸载时 revoke，不把受保护资源裸塞进 `<img src>`。右侧栏和消息 chip 显示“待按需理解 / 已理解 · N 个元素 / 视觉不可用 · 已降级 / 旧附件 · 仅文本”等状态，图片不显示误导性的“0 字”。deep_read/ask_papers 返回后会原位更新附件状态。
- **流式渲染性能**：历史消息 `ChatMessage` 全部 `React.memo`（流式期间 msg 身份不变 → 旧消息零重渲染，markdown 不重解析）；thinking/answer delta 先入缓冲，**60ms 合帧**（~16 次/秒）再写 store（per-token setState 是卡顿与"成段吐出"的根因）；终态事件立即冲刷保证时序；`handleSend`/`handleRegenerate` 经 `useChatStore.getState()` 取 action 保持引用稳定（memo 不被回调身份击穿）；自动滚动仅在用户已贴近底部（240px）时触发。
- **谱系图 v2**（`components/GenealogyGraph.tsx` + `lib/genealogy-layout.ts`，纯 SVG 零依赖）：**确定性布局纯函数**（输入确定→输出确定，Node 单测覆盖）：920px 固定内容宽、年份等距刻度（按年份序号而非真实间隔）、泳道按簇大小降序且行预算固定（同 (簇,年) 桶按被引取 top3，超编折叠为 "+N" 聚合节点，点击展开桶列表）、节点半径 3 档；920×560 固定视口 + fit-to-view + 滚轮缩放/拖拽平移/复位。**详情面板**（点节点展开）：元信息+角色徽章+摘要片段、引用关系双列（它引用的/被引用的，仅库内，点击跳转聚焦）、原文链接、**「深问这篇」**（经 `stores/ui.ts` 的 composerDraft 预填聊天输入框，打通图谱→RAG 问答）。**思想源流高亮**：聚焦节点时祖先引用链按年份渐变粗细（越老越粗）。顶部簇筛选 chips/候选集/语义边开关，底部谱系摘要统计行（核心/候选/引用边/语义边/奠基/桥梁）。
- **设计**：「纸墨书院」令牌（`app/globals.css`）：宣纸底 + 墨色 + 黛青主色 + 朱砂点缀，serif 展示标题，tabular-nums 数字，亮/暗双主题（localStorage 持久化）。
- **状态**：`stores/chat.ts`（会话+流式暂存+主题/语言偏好）、`stores/ui.ts`（边栏开合 + composerDraft 一次性聊天草稿）、`stores/auth.ts`（鉴权态：`/auth/config` + localStorage 令牌，mount 后 hydrate，SSR/水合安全）。
- **多用户**：`/login` 根据 `REGISTRATION_OPEN/GUEST_ACCESS` 决定注册和游客入口；生产禁游客时 AppShell 强制未登录浏览器跳转。Nav 仅对 administrator 显示管理入口；`/admin/agent-keys` 提供创建（明文一次）、复制、列出、撤销长期 Agent Key及密码修改。所有 API 经 `authHeaders()` 自动带 Bearer 或（允许时）游客标识；预水合渲染确定性加载屏。
- **SSE**：直连后端（`NEXT_PUBLIC_BACKEND_URL`，构建时注入）绕过代理缓冲；REST 走 Next 代理。
- **历史恢复**：`lib/history-loader.ts` 恢复消息并把 map/review/summaries/path 注入对应 tool_call 渲染卡片。

---

## 12. 持久化与安全

- **会话**：`history_record/chat_<ts>_<slug>.json`（basename 防穿越；重命名只改文件内 title）；trace JSONL 于 `history_record/trace/`。
- **SQLite**（`data/metadata.db`）：papers 元数据、summary_cache、全局 `paper_elements` 与 `vision_cache`——均是降本/重建缓存，不作为跨会话记忆授权。`doc_fingerprint` 防止 PDF 变化后复用陈旧元素。`data/users.db`：多用户账号与令牌（§3.4）。
- **向量库**（`data/chroma/`）：L1/L2 会话隔离索引 + L3 全局 elements 缓存；L3 检索由调用方当前 paper/upload ID 集合强制收窄。
- **上传/产物**：`data/uploads/` 同时保存用户原件 `<uuid>.<ext>` 与文本 sidecar `<uuid>.txt`；`data/assets/upload_*` 保存上传图片/DOCX media 等元素资产；`data/exports/` 保存 `.md/.txt/.docx/.tex` 产物。原始 bytes 从不写入历史 JSON。Web 上传与导出额外写入被 `.gitignore` 排除的 `data/web_artifacts.db` 所有者索引；原始文件、文本、图像资产和导出文件读取前按当前账号/游客身份校验。OpenAI API 私有文件不进入这些目录，而是按会话隔离在 `data/openai_api/`；API 公共别名保持短期可公开读取。
- **安全**：SSRF 防护（URL 下载统一走公网校验）、50MB URL/20MB 网页直传上限、UUID/file_id/basename/后缀收敛、图片 raw endpoint 仅开放安全 raster MIME、CORS 显式白名单（永不 `*`）、/v1 Bearer 常量时间比较、Web 附件/导出/上传元素按所有者校验、错误不回显 OS 路径；口令 PBKDF2 加盐哈希、令牌只存哈希、历史按账号隔离（§3.4）。图表裁图与扫描页在按需理解时会发送到配置的第三方 VLM，未配置时优雅降级。

### 部署单 worker 与事件循环隔离

`/v1`、网页 `/api/v1` 和健康检查共享同一个 Uvicorn 事件循环；会话内存、熔断器、LLM 限流信号量也都是进程内状态，因此仍必须 `--workers 1`。同步的 Docling/PyMuPDF 结构解析、SentenceTransformer/CrossEncoder 推理、Chroma 向量读写和附件快速提取不得直接运行在 async 请求链：统一经 `core.blocking.run_cpu_bound` 投递到一个进程级 daemon ML worker 串行执行。SQLite Checkpoint、轻量文件写入等 I/O 经 `core.blocking.run_io_bound` 投递到独立的两个受限 I/O worker，避免 ML 队列形成跨请求队头阻塞。OpenAI-compatible 流式链路验证后先发 role 首帧；工具进度经 reasoning 输出，95 秒软时限停止启动新工作，105 秒硬时限前闭合 stop + `[DONE]`，客户端断开会取消并回收 producer。原生 ML 调用被取消时无法强杀，但已取消的等待任务不再写入请求状态。扩多 worker/多机前仍须外置共享会话、任务队列、熔断和限流状态，不能直接增加 Uvicorn worker。

前端 `fetchAuthConfig` 使用 8 秒超时；超时只结束首屏无限 spinner，并保留本地已有登录信息。所有 `/api/v1` 权限仍由后端逐请求校验，前端降级不构成授权。

---

## 13. 配置与测试

- `config/settings.yaml`：llm（base_url/model_light/model_reasoning/temperature/timeout，.env `DEEPSEEK_*` 覆盖）、search（每源结果数/源开关）、reader（pdf_dir/parser/read_chunk_chars）、storage（sqlite/chroma/embedding_model）。
- 环境变量：`DEEPSEEK_*`、`MULTIMODAL_*`、`S2_API_KEY`、`OPENALEX_EMAIL`、`CROSSREF_EMAIL`、迁移/应急 `AGENT_API_KEY`、`PUBLIC_BASE_URL`、`AUTH_REQUIRED/REGISTRATION_OPEN/GUEST_ACCESS`、`FRONTEND_ORIGIN/CORS_ORIGINS`、`APP_HOST/APP_PORT`、`BACKEND_URL/NEXT_PUBLIC_BACKEND_URL`、`HF_ENDPOINT/HF_HUB_OFFLINE/XDG_CACHE_HOME`。
- 测试：`./.env_conda/bin/python -m pytest tests/ -q` 默认运行非 slow 回归，普通测试须 stub 外部 LLM/VLM/Docling，不依赖凭据、网络或模型下载；slow 用例通过 `-m slow` 单独运行。`tests/eval/` 提供检索质量黄金集（offline fixture / online 双模式）；前端以 `pnpm build` 作为类型与生产构建闸。真实 API + 浏览器端到端验证属于发布前手动验收，不进入普通 CI。

## 运行时论文渠道与远程全文策略

论文检索的实时策略存放在 `data/users.db` 的 `paper_search_policy` 单行表中；`config/settings.yaml` 只负责首次种子。管理员通过独立页面 `/admin/paper-search` 修改后，约 5 秒缓存立即失效，下一次检索生效。策略使用版本乐观锁，不包含 API key、Authorization 或完整配置邮箱。

### 快速部分结果与按源熔断

`SearchManager` 只调度管理员启用的 9 个元数据源，并同时执行单条渠道时限（默认 12 秒）和全局检索时限（默认 30 秒）。总时限到达后取消并 drain 未完成任务，保留已返回论文继续去重和重排。主检索后端遇到 429 最多短重试一次，等待上限 2 秒，不再让云数据中心 IP 的持续限流占满整个 turn。

每源维护单 worker 进程内健康状态：连续 3 次 429、超时、连接错误、5xx 或非法响应后熔断 300 秒；冷却后只放行一条 half-open 查询，成功关闭熔断，失败重新打开。HTTP 200 合法空结果、管理员关闭和缺少可选 key 不计入故障。管理员开关优先于熔断，熔断不会永久改写配置。

### 四档远程全文访问

`paper_fetch_mode` 的判定顺序为：管理员全文策略 → 来源开关 → 来源熔断 → 本地缓存 → 调用来源。

- `enabled`：允许 OA 候选解析、文件头探测、自动 `_ensure_fulltext` 和直接 `deep_read` 下载。
- `explicit_only`：允许候选解析和文件头探测；直接 `deep_read` 可下载，自动全文升级不得下载。
- `probe_only`：允许候选解析和轻量探测；所有普通用户路径禁止完整 PDF 下载。
- `disabled`：禁止远程候选解析、Unpaywall/doi.org 全文解析、探测和下载。

所有模式都优先复用已通过 `%PDF-` 校验的本地 PDF、已持久化结构和元素理解，策略切换不删除数据。来源开关关闭后不使用该来源的直接 PDF URL；Unpaywall/doi.org 作为独立 OA 辅助边界保留，除非全文总策略为 `disabled`。

`fetch_policy_disclosure=affected_only` 只在当前请求确实被限制时生成管理员策略提示；`silent` 不向 web 进度、`/v1` reasoning、工具卡或正文注入策略原因，但仍向模型提供真实证据层级，禁止把摘要冒充全文。原始 provider thinking 继续按既有不变量透传，不增加事后过滤器。

### 管理员诊断

管理员接口位于 `/api/v1/admin/paper-search/*`。连通性检测使用固定查询，报告 HTTP 总延迟、真实结果数和可选 PDF 文件头探测，不伪造 DNS/TCP/TLS 分段。下载测速与连接检测分开，管理员可显式测试已关闭渠道或全文关闭状态；目标来自固定登记或经过 SSRF 校验的诊断候选，不接受任意 URL。测速优先 Range，最多读取 1 MiB、单项最多 20 秒、校验 `%PDF-`，不写生产 PDF 目录。数据库只保留最近一次完整连接/测速结果和最多 20 条摘要，不保存响应正文、完整 PDF 或秘密。
