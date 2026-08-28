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
│  pdf/     上传文件结构解析 | Docling/PyMuPDF | 元素裁图         │
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
│   ├── reader_agent.py         # 上传全文/网络摘要 → 分层提取（并发+缓存+分段）
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
│   ├── session_memory.py       # /v1 请求 messages 临时文本历史重建（见 §7）
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
│   ├── pdf/                    # 退役网络 fetch shim | 上传结构解析(Docling/PyMuPDF) | caption
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

### 使用文档公告页

`GET /api/v1/usage-document` 提供全局公开的 Markdown 使用文档，前端页面为 `/usage-doc`，聊天顶部 Nav 始终显示入口。文档正文与版本信息存储在 `data/users.db` 的单行 `usage_document` 表中，首次读取自动 seed 默认功能说明；保存使用 `expected_version` 乐观锁，避免多个管理员标签页互相覆盖。

### 用户反馈页

`POST /api/v1/feedback` 是用户反馈提交入口（前端页面 `/feedback`，Nav「反馈」按钮直达）：登录用户、游客（`X-Guest-Id`）与本地模式均可提交，body 为严格 Pydantic（`category` ∈ 问题报告/功能建议/其他，`content` 1..4000 必填，`contact` ≤120 可选）。反馈记录存于 `data/users.db` 的 `feedback` 表（`core/feedback_store.py`，追加型记录，含提交者 id/用户名/角色、`open`/`resolved` 状态与处理审计字段）；同一提交者 60 秒内重复提交返回 429。反馈内容仅管理员可见：`GET /admin/feedback`（status 筛选 + 分页 + 全局计数）、`PUT /admin/feedback/{id}`（标记已处理/重新打开）、`DELETE /admin/feedback/{id}`，均通过 `_administrator` 鉴权，前端管理页为 `/admin/feedback`。

管理员在同一页面看到一个纯 Markdown 文本框，以及独立的“上传图片并插入”按钮；上传接口为 `POST /api/v1/admin/usage-document/assets`，仅管理员可用，图片写入 `data/usage_document/assets/`，服务端生成 UUID 文件名并只接受 PNG/JPEG/GIF/WebP raster 文件（单文件 ≤10MB）。上传响应返回 Markdown 图片语法，前端按当前 textarea 光标位置插入；普通用户、游客和未登录访问者均只能读取。文档渲染复用 `react-markdown` + `remark-gfm`，不启用原始 HTML，链接协议和图片地址经过安全过滤。

管理员保存接口为 `PUT /api/v1/admin/usage-document`；认证失败返回 403，版本冲突返回 409，内容长度上限为 500,000 字符。公告文档与聊天历史、用户上传、OpenAI API 私有存储相互隔离。

默认内容来自 git 跟踪的 `config/usage_document.md`（`core/usage_document_store.py` 启动时读取），因此手册可以走"本地编辑 → push GitHub → 服务器 `git pull` 后执行 `scripts/sync_usage_document.py`"的分发流程：脚本用仓库文件覆盖数据库行（幂等，内容一致时不涨版本），后端 5 秒读缓存内生效、无需重启；覆盖会丢弃服务器上通过管理页面临时编辑的内容，页面上传的图片也属服务器运行时数据（`data/usage_document/assets/`），不随 git 分发。

前端页面为两栏布局：左侧按 Markdown 标题自动生成"本页目录"侧栏（`frontend/components/UsageDocOutline.tsx`，滚动时高亮当前章节，窄屏折叠为可展开目录）；正文标题渲染时按行号挂锚点 id。手册行首的一级标题与页面固定大标题重复，渲染时被跳过，全页只保留页头一个"使用文档"大标题。

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

- **上下文窗口**：会话内完整历史在 `session.messages`；发给 LLM 前超窗压缩（保留最近 8 条 + 摘要 SystemMessage）。跨会话无任何记忆。`Session Context` 会持续携带核心集 + 候选集的稳定 `paper_id` 目录，因此用户点名网络论文（含候选集 DOI/标题）时模型必须直接调摘要级 `ask_papers`，严禁再次 search_papers“定位”；只有点名当前会话上传附件时才可调 `deep_read`。
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
| `search_papers` | topic 必填；conception/language 可选 | §5。产物：核心集+候选集+子方向；topic 命中会话已有论文（id/DOI/精确标题）时 fast-fail 并引导直接用摘要级 `ask_papers`，需要全文则上传文件 |
| `deep_read` | attachment_ids/focus 可选 | §6。仅处理用户上传 PDF/DOCX/TXT/MD/TEX/图片并按需多模态；旧网络 `paper_ids` 调用被拒绝 |
| `ask_papers` | query 必填；paper_id/attachment_id/top_k 可选 | §8.2。网络论文仅检索当前有效摘要，上传附件检索完整 sidecar/元素；paper_id 与 attachment_id 互斥 |
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
- **前置守卫**：无网络论文时 research_map/reading_path 返回 NO_PAPERS；write_review 在既无合格网络摘要也无上传附件时返回 NO_PAPERS；deep_read/check_structure/check_format 在无上传附件时返回 NO_PAPERS 并提示上传文件。
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

`check_format` 同为零 LLM 确定性工具，双模式：纯文本模式（图表编号连续性/正文引用、引用风格混用、GB/T 7714 类型标识命中率、关键词数量、多级标题断号）与 LaTeX 模式（自动识别，查 `\cite` 无参考文献块、`\ref` 无 `\label`、abstract 环境）。`spec` 参数接收用户粘贴的格式要求原文，关键词规则引擎映射可文本化检查项（摘要字数/关键词个数/正文规模/GB/T 7714）逐条对照；字体/行距/页边距等排版项**诚实降级**为"需 Word/LaTeX 人工核对"清单。`export_manuscript` 把写作产物（初稿/润色稿/修改清单）的 markdown 转为 docx（python-docx：标题/列表/加粗/表格）、tex（ctexart 中文可编译）或 md，写入 `data/exports/`。`GET /files/{filename}` 仅接受 basename 及 `.md/.txt/.docx/.tex/.svg/.html` 白名单，使用 `FileResponse` 返回正确 MIME 和 RFC 5987 兼容的 UTF-8 `Content-Disposition`，因此长中文 DOCX 文件名也可直接下载；清小搭渠道经 x_soda 附件通道下发（§7.5）。

### 4.4 对话 SSE 事件

`step` / `thinking`(delta) / `answer`(delta) / `tool_start` / `tool_result` / `tool_warning` / `tool_progress` / `heartbeat`(15s) / `history_saved` / `done`(thinking/answer/tool_calls 瘦身/trace_id/usage) / `error`。

---

## 5. 搜索子系统（`agents/search_agent.py` + `tools/search/`）

```
topic (+conception)
  → ① 意图理解（1 次 LLM，10 秒子超时，超时回退原始主题）：还原缩写/短主题 → research_goal
      + 2-4 个子方向 × 中英检索式；JSON 容错解析，失败回退 [topic]
  → ② SearchManager.search_all（启用源 × 查询并发，deadline = min(管理员检索总时限, 工具预算剩余−8s)，去重）
  → ③ 语义重排 rerank_papers：
      时间充足时 score = 0.55*cos(本地 MiniLM) + 0.20*log被引 + 0.15*时效 + 0.10*token重叠
      嵌入不可用或共享 deadline 余时不足时，立即使用 0.65*token重叠 + 0.20*log被引 + 0.15*时效的确定性快速重排
  → ④ 自适应分层 adaptive_tier：
      候选集 = score ≥ max(0.35, top1*0.45)，封顶 25 篇
      核心集 = 候选集头部，在 [5,12] 内找最大相对分数断崖（elbow≥0.25）截断
  → ⑤ 写元数据/摘要 SQLite 缓存 + 会话向量库 L1（title+当前有效摘要）
```

- 无 X/Y/Z 参数、无 Flash/Pro 模式——分层是确定性算法，不花 LLM。
- **数据源与限流**（`tools/search/registry.py`、`tools/search/base.py::RateLimiter`）：来源集中登记协议、许可、路由标签、配置门禁和每轮查询预算。SearchManager 按来源创建一个批处理任务，而不是“来源×查询式”任务；arXiv 单连接/3 秒一次，PubMed 按 NCBI 3/10 RPS，OpenAIRE 按匿名/认证小时额度，其他源使用保守进程级 limiter。共享 `httpx.AsyncClient` 使用 `trust_env=False`。`SearchOutcome` 分别记录真实请求数、限流头、最终域名、重定向以及 queue/connect/read/network 耗时，并区分远端 timeout/429/schema/redirect/challenge 与本地 `local_budget_exhausted`；来源级结果只写当轮诊断与路由记录，不维护临时熔断状态。默认 smart 路由最多 4 个主渠道、2 个兜底渠道，学科、预印本、机构仓储和 dataset/software/report/thesis/DOI 意图使用不同的确定性矩阵。bioRxiv/medRxiv 使用官方 metadata API + 本地 FTS5，不抓网页；各来源只提供元数据和其官方接口返回的摘要，不解析或下载远程 PDF。详细许可见 [Official_Paper_Platform_License_Description.md](Official_Paper_Platform_License_Description.md)。
- **去重**：DOI 精确 → 标题归一化精确/模糊（Jaccard≥0.95）→ 字段合并（摘要取长、被引取大、来源取并集）。
- 搜索结果注入 LLM 上下文时用 `<search_results>` 定界标记（数据非指令）。
- **部分成功优先**：来源 wave 使用同一绝对 deadline；到点取消未完成 task，但保留已完成来源。分层一完成就把核心/候选集发布为 session snapshot，后续元数据 SQLite/Chroma 增强是可跳过步骤；外层 timeout 从 snapshot 组装 partial，而不是把已检索论文作废。
- **证据边界**：搜索结果可以展示全部论文元数据，但问答、地图摘要和综述只纳入当前能力策略允许且非空的摘要。标题、旧 summary、PDF URL、历史全文状态和模型常识都不能替代缺失摘要。

### 5.4 可靠性质检（`tools/search/integrity.py`，确定性 · 零 LLM）

`integrity_sweep(paper_ids?)` 是"写综述前/投稿前必跑"的质检闸。逐篇（并发 + 既有限流器）：
- 有 DOI → OpenAlex `GET /works/doi:{doi}?select=is_retracted,is_paratext,merged_into` 读撤稿标志（复用 `openalex_refs` 单条 DOI 查询模式 + `_mailto`）；Crossref `GET /works/{doi}` 读 `relation`（`has-retraction`/`has-correction`/`has-expression-of-concern`，表示本文是被处置方）+ `assertion`（复用 `export/enrich` 请求模式）。
- arXiv 篇（`source=="arxiv"`）→ 有 DOI 即提示"已有正式版"；无 DOI 的取前 5 篇查 arXiv `journal_ref`（受 3s/请求 ToU 约束，封顶避免拖慢）。
- 分级：✅ 无异常 / ⚠️ 勘误或关切 / ⛔ 撤稿（附通知来源）/ 🔁 预印本已正式发表 / ❓ 未查到（无 DOI 或查询失败，诚实降级，不算错误）。复用 `RateLimiter` 与匿名邮箱策略。

### 5.5 文献库导入（`tools/export/bibtex_import.py`，确定性 · 零 LLM）

`bib_import(attachment?)` 与 `citation_export` 构成双向互通。上传通道放行 `.bib`（`ingest.extract_text` 文本透传，本质是文本，无需新下载器）。纯 Python 容错解析器（`@type{key, field={...}/"..."}`，嵌套花括号/引号，不引新依赖）逐条提取 title/author(按 `and` 切)/year/doi/venue/abstract；有 DOI 的经 `enrich_by_dois` 补全；与会话既有论文去重（`generate_paper_id` ID/DOI 相等 → `title_similarity≥0.95` 标题模糊）后并入 `candidates`（不污染核心集）并写会话向量索引 L1。输出导入报告（成功/补全/跳过及原因）。

---

## 6. 上传全文子系统（`agents/reader_agent.py` + `tools/pdf/`）

### 6.1 证据策略

- `deep_read` 只接受当前会话的上传附件 ID；旧调用若传网络 `paper_ids` 会被拒绝，并提示上传原文。
- `ask_papers` 对网络论文只检索标题、当前有效摘要和摘要级结构信息；精确数字、实验设置、章节结构等摘要不足的问题返回证据不足，不触发联网全文升级。
- 用户上传 PDF/DOCX/TXT/MD/TEX 后，完整 sidecar 与结构解析结果进入会话 RAG；图片以及 PDF/DOCX 内嵌图表按需运行 OCR/VLM，并复用文档指纹与图像哈希缓存。
- 旧网络全文 summary、PDF 路径和状态不作为证据；版本化退役迁移负责删除这些衍生缓存。

### 6.2 上传论文 PDF → RAG 多模态管线

`agents/reader_agent.py::parse_and_understand` 是上传 PDF/DOCX 结构化理解的唯一 chokepoint，`deep_read` 和附件定向问答复用同一份理解缓存。

1. **结构解析**（`tools/pdf/structure/`）：`get_structure_parser()` 默认使用 Docling（布局、OCR、表格结构），失败自动回退 PyMuPDF。输出 `ParsedPaperDocument`：带页码章节、图/表/公式 `PaperElement` 清单、bbox、PNG 裁图路径、`image_hash`、原始文本、扫描标记与 PDF SHA-256 `doc_fingerprint`。旧 `tools/pdf/parser.py` 不再存在。
2. **扫描页恢复**（`core/multimodal/ocr.py`）：`is_scanned` 时对结构层未恢复的前几页做 VLM-OCR；视觉服务不可用或失败时保留现有文本，不再因 `no_text` 硬失败。
3. **元素语义理解**（`core/multimodal/analyzers.py`）：有界并发、逐元素容错，按图 > 表 > 公式排序，并受 `ELEMENTS_PER_PAPER_CAP` / `VISION_CALLS_PER_PAPER` 硬预算约束。图像哈希缓存命中免费且不占调用预算；表格优先保留 Docling TableFormer markdown，公式允许 VLM LaTeX 修正结构层提取。
4. **全局持久化**：SQLite `paper_elements` 以 `doc_fingerprint` 判断新旧；匹配时直接恢复已理解元素，并补建可能丢失的全局 `elements` Chroma 索引，不重新 parse 后续语义、不调 VLM、不重写数据库。轻量元素引用进入摘要。
5. **会话文本索引**：网络摘要进入 L1；上传全文按章节父子切块进入 L2，均带 `session_id`。元素向量可全局缓存，但检索时只允许当前会话的上传文档 ID 集合。

系统不从论文平台探测或下载 PDF。HTTP(S) 下载仅用于用户在 `/v1` 请求中明确提供的附件 URL，并在保存为当前会话私有上传前执行 SSRF、大小、后缀与 MIME 安全校验。

### 6.3 上传附件的按需多模态

上传附件是独立数据域，不伪装成论文：不会进入 `session.papers`、`session.candidates` 或 `paper_summaries`。文本 chunk 使用原始 `<attachment_id>`；元素文档使用 `upload:<attachment_id>` 命名空间。

- **PDF**：用户上传原件按需交给 `parse_and_understand`，获得布局、OCR、元素理解与私有缓存；网络论文永不进入该解析路径。
- **DOCX**：上传时提取段落和表格 markdown；按需阶段从 `word/media/*` 提取内嵌图片为 figure 元素并进行视觉理解。
- **PNG/JPG/JPEG/WebP**：按单个 figure 元素处理，保留真实 MIME 发送给 VLM。
- **纯文本格式**：TEX/TXT/MD/BIB 直接使用 sidecar，不调用视觉模型。
- **缓存与降级**：上传同样复用 `paper_elements`、`vision_cache` 和全局 elements 向量；理解后的元素文本写回 sidecar 并重建当前会话文本索引。无 VLM、熔断或单元素失败时降级为文本/caption/结构提取，不阻塞读取。

全局缓存不等于全局授权：检索范围始终是当前会话网络论文 ID + 当前会话附件的 `upload:<id>`；`explain_element` 也先验证该论文或附件确属当前会话，不能仅凭 SQLite 中存在记录跨会话读取。

### 6.4 图表导览与元素解读

`exhibit_index(attachment_ids?)` 只处理当前会话的用户上传文件，并在需要时先完成 PDF/DOCX/图片的结构与元素理解。`explain_element(element_id, paper_id?)` 只读取这些上传文件产生的 SQLite 元素记录，包括 `understanding`、结构提取、页码、章节、caption 与受限图片 URL。网络搜索论文不进入元素管线；需要图表或公式级分析时必须上传原文。前端点击上传元素可继续追问；视觉不可用时仍返回可获得的结构/caption 信息。

---

## 7. OpenAI 兼容层（`/v1`，清小搭接入）

| 端点 | 说明 |
|------|------|
| `GET /v1/models` | 连通性 + 凭证校验，返回 `paper-agent` |
| `POST /v1/chat/completions` | 内部跑 chat_turn；stream=false 返回标准 JSON；stream=true 真流式 SSE |

### 7.1 真流式帧序

role 帧 → `delta.reasoning`（provider 原生 reasoning、显式 `<thinking>`、工具调用前说明、带参 emoji 进度文案与 15s 心跳）→ `delta.content`（**Markdown 卡片块** 与 answer 增量，见 §7.7）→ stop 帧（finish_reason=stop/length + usage + x_soda）→ `data: [DONE]`。帧随 agent 事件即时转发（非跑完重放），流式层不改写 provider 原始 reasoning；reasoning 各段之间由通道插入显式换行分隔（模型思考与每个工具模块之间空一行、模块内每条进度独占一行、思考恢复再起一段），客户端拼接 `delta.reasoning` 后仍是分段排版。`use_skill` 的公开工具开始/结果事件仍被忽略，但技能**新加载**成功会触发专用 `skill_loaded` 内部事件（只携带技能名），`/v1` 通道将其同时映射为思考折叠提示（`📘 已加载技能《标题》，按其工作流执行…`）与正文技能行（`━━ 📘 技能 · 标题 ━━`，可经展示策略关闭），web 通道原样转发该命名事件而前端忽略——自制前端行为不变。未产出内容即失败 → HTTP 5xx；流式中途出错 → stop 帧 + error 字段 + [DONE]。

**alias 一致性不变量**：卡片块与技能行作为 `delta.content` 的一部分进入 `content_parts` 与 `finalize_turn` 的 `final_answer`，保证下一轮 `canonical_message_chain` 与清小搭回传的 assistant 内容逐字一致——否则跨轮会话续接会断裂。卡片与回答共享 `max_tokens*4` 内容预算，且卡片占比被硬性限制在 60% 以内；预算过小（<2000 字符）时直接跳过卡片。

### 7.2 API 独立存储、会话身份与 7 天 Checkpoint

`/v1` 不再依赖首句哈希的 2 小时内存 SessionMemory。鉴权先得到不含明文 Key 的 principal（数据库 `key_id/created_by/source`；应急/开发 token 只使用单向摘要）。清小搭提供 `sessionId` 时，以 `HMAC(secret, credential_id + sessionId)` 作为稳定主别名：原始值不落库、不同 credential 永不共享，并在首次请求准备阶段立即写入 alias，因此 response 尚未正常结束、但工具已写过部分 Checkpoint 时，下一轮仍可恢复。没有 `sessionId` 才使用 `credential_id + optional OpenAI user + canonical prior messages` 的消息链 alias；当前最后一条 user 不参与 lookup，正常响应后以实际 assistant 内容建立下一轮 alias。相同开场的首轮不会共享；一个消息链 alias 指向多个 session 时标记 ambiguous，后续拒绝猜测并新建会话。

API 工作状态写入 `data/openai_api/state.db`：版本化 JSON + zlib，未压缩总量上限 8 MiB，单字段超过 256 KiB 外置为私有 `state_payload` artifact。只保存 topic/profile、论文候选、结构化摘要、地图/路径/综述、附件 artifact 引用、loaded skills、深读计数和 RAG session id；**不保存完整 messages、reasoning/思维链、API Key、raw bytes、完整 PDF 正文或默认 Trace**。TTL 默认 7 天，SQLite optimistic version + 单进程 asyncio lock 防止陈旧 writer 覆盖。每次请求无论新建还是恢复 Checkpoint，都从调用方完整可见 `messages` 重建临时 `session.messages`，只排除本轮最后一条 user；因此短确认能看到上一轮助手提议，而这些文本仍不会进入 Checkpoint。

API 物理根目录固定隔离为 `data/openai_api/{state.db,metadata.db,chroma,blobs,tmp,traces}`。web 继续使用原 `history_record`、`data/uploads`、`data/pdfs`、`data/assets`、`data/exports` 和 web Chroma；API 清理器没有权限扫描这些路径。

### 7.3 整轮预算、最少工具门禁与部分成功

`tool_budget_policy` 除逐工具预算和 `reserve_seconds` 外，还持久化 `api_turn_soft_seconds`（默认 95）与 `api_turn_hard_seconds`（默认 105，两者均由旧库幂等 `ALTER TABLE` 补列）。软/硬时限都在 `/admin/performance` 可调、无固定数值上限，唯一约束是 `软 ≤ 硬 − 5`（为软超时后的降级总结收尾留时间；硬最小 35），逐工具预算与默认预算的上限动态跟随硬时限。`/v1` 请求只读取一次完整策略快照并创建 `TurnExecutionContext`，所以同一轮的软硬时限、逐工具预算和预留量一致，运行时再防御性钳制 `硬 ≥ 软 + 5` 兜底手工改库；经清小搭网关转发的请求仍受网关 120 秒总超时限制，超过 120 秒的设置只有直连 `/v1` 的调用能真正用满；Web 仍使用固定 240/300 秒软硬时限。每次模型决策前追加不持久化的动态预算提示。

仅 `openai_api` 启用执行门禁：一个模型决策批次最多一个公开工具、整轮最多三个；首个工具在轻/中/重 5/10/15 秒最低启动阈值以上可裁剪到“剩余软时限−预留”，后续工具必须完整容纳管理员预算 + 预留。`search_papers`/`research_map` 仍各自最多实际启动一次。任一 TIMEOUT 或携带 `budget_exhausted` 的 partial 关闭后续公开工具准入，模型只做最终总结；`use_skill` 是内部加载事件，不占公开工具额度。

`ToolInvocationContext` 把外层算出的真实 deadline 传入检索。`SearchManager` 到点取消未完成来源但返回已完成来源；检索在分层后立即把 papers/candidates 写入 session 并发布 snapshot。余时不足时用无 embedding 的确定性重排，并跳过 OA 探测、SQLite/Chroma 持久化。若内部或外层时限随后触发，`search_papers` 返回 `status=partial`、`budget_exhausted=true`、完成/跳过阶段及 configured/effective timeout；orchestrator 把有状态变化的 partial 与 success 一样写 Checkpoint，`/v1` 仍先输出标准检索表再总结。SSE 帧序不变。

### 7.4 多模态输入（`backend/app/api/v1/multimodal.py`）

content 数组解析：`text` 直取；`file.url` 存在时始终作为当前请求的实际下载地址，即使同时携带 `file_id` 也不会被后者阻断；`file_id` 仅作为清小搭来源标识写入轻量附件元数据，不参与路径拼接、本地查找或 URL 推导。仅有 `file_id` 时记录 `reason_code=missing_download_url` 的安全结构化日志（不记录完整请求、URL、文件内容或标识值），不发起网络请求，并把“缺少可下载 `file.url`”降级说明放入本轮上下文。缺失 `filename` 时仅从 URL path 的安全 basename 回退，URL query 不进入文件名或错误文本。

`file` 与 `image_url` 统一经过 `save_attachment`。常规格式支持 PDF/DOCX/TEX/TXT/MD/BIB/PNG/JPG/JPEG/WebP；API 额外接收 DOC/XLS/XLSX，但只保存原件并创建空文本 sidecar，状态固定为 `deferred`，不会调用 PDF、Docling、DOCX 或 VLM 解析链，后续 `deep_read`/图表工具返回明确延期原因而不是把空文本冒充解析成功或抛出 500。PPT/PPTX 和未知后缀继续拒绝；显式 filename 后缀优先于不规范 MIME，避免把 `.ppt` 伪装成 Word/PDF。Web `/chat/upload` 的格式和 20 MiB 上限完全不变。

HTTP(S) URL 逐 redirect 做 SSRF/公网地址校验并流式写入 0600 temp，`trust_env=False`；每次请求从 `api_storage_policy.max_upload_bytes` 读取动态上限（默认/最大 200 MiB、最小 1 MiB），下载流与私有附件保存执行同一限制，超限/中断删除 temp，错误显示实际配置值且不回显签名 query。成功后转为当前 session 的私有 artifact；也支持 `data:` URI。只把附件引用登记到当前 `/v1` Checkpoint 并建立 API 专属文本索引，**摄取阶段零 VLM**。私有上传绝不跨 session 自动共享；论文平台搜索结果不会进入该附件下载路径。后续 `deep_read`、附件问答或图表请求才按需理解并复用缓存。`input_audio` 仍明确不支持并要求文字转写。

### 7.5 文件产物输出（`x_soda.attachments`）

本轮成功调用 research_map/write_review 时，`tools/export/report.py` 写入 API 独立 export artifact；research_map 先构建共享的确定性研究图谱视图，再按 `/admin/display-policy` 的四个开关输出美化 SVG、可下载自包含 HTML 和/或 Markdown 关系说明。正文 Mermaid 不进入 artifact，而是在研究地图状态行后、最终回答前作为完整 fenced `flowchart LR` 源码块进入 assistant content。SVG 使用标题块/彩色簇泳道标签/年份分组/奠基与候选标记/关系图例，每篇论文独立成卡（同年同主题纵向堆叠，不折叠），与前端 `GenealogyGraph` 共享节点关系语义；HTML 使用 CSP、内联 CSS/数据/原生 JavaScript，支持筛选、平移缩放、边切换和节点详情，下载后才执行。Markdown 列出主题摘要、论文、DOI/来源和引用边且不嵌 Mermaid。非流式附件挂响应顶层，流式只挂唯一 stop 帧：`{fileUrl, fileName, fileType, mimeType, fileSize}`（4 必填+size，image 类自动补可选 `previewUrl`）；展示名与物理 hash 路径分离，每次导出产生唯一 public alias；fileUrl 由请求 base URL 拼 `/files/{alias}`，清小搭负责转存。`/files` 先解析未过期 API alias，再回退 web exports；过期 API alias 固定 404，`.html` 与 SVG 一样受 basename/MIME 白名单保护且以 attachment 下载。此外，`data.files` 型工具结果（export_report / export_manuscript）在当轮同样转换为附件下发——显式 `export_report` 不读取展示策略，固定导出美化 SVG + Markdown。

三类工具结果还会追加**当轮即时附件**（`openai_compat._extra_attachments`，均走同一 export artifact 管道与 24h TTL）：`explain_element` 的图/表裁剪图 PNG（落盘位置按 `settings.reader.assets_dir` 解析，且重新校验该文档属于当前会话的元素 scope，会话隔离红线在附件层二次生效）；`citation_export` 的引用文件——GB/T 7714 恒为 `.txt`，BibTeX 按展示策略 `bibtex_export_mode` 在 `.bib`、内容与 `.bib` 逐字节一致的 `.md` 副本（`text/markdown`，下载后改回扩展名即可使用）或两者之间选择，默认 `md_only`（清小搭侧无法下载 `.bib`）；`field_census` 的纯 SVG 趋势图（年度折线 + 高产作者/机构横条，`tools/export/cards.py::render_field_census_svg`）。只要本轮 BibTeX 导出实际借助了 `.md` 附件（模式非 `bib_only` 且确有非空 BibTeX 结果），通道在正式输出末尾追加一行固定说明（`cards.BIBTEX_MD_EXPORT_NOTE`：清小搭侧暂不支持下载 `.bib`，请手动转存），该行经 `emit_content`/content 拼接进入流式 `delta.content`、非流式 `message.content` 与 checkpoint 回显，保持别名链一致；它解释附件行为而非装饰卡片，不受 `tool_cards_enabled` 门控，预算耗尽或错误/超时收尾的轮次自然不追加。

协议边界必须明确：`openai-compatible-agent-integration-guide.md` 只定义正文/推理增量和 `x_soda.attachments`，没有任意 React 工具卡或交互图谱组件协议。因此自有前端的 `SearchResultCard`、`ResearchMapCard`、`GenealogyGraph` 等不能原样出现在清小搭；清小搭接收 Agent 的自然语言总结、一行式工具状态行与检索结果表格（§7.7）、正文 Mermaid 源码块及附件文件卡。主站内的 `GenealogyGraph` 交互仍只由本项目 `/chat` 提供；可下载 HTML 是普通附件，下载后在独立浏览器页面执行，不发送私有未声明字段冒充兼容能力。

### 7.6 生命周期、磁盘压力与 API Trace

默认保留：session/upload 7 天、export 24 小时、语义/视觉缓存 90 天、Trace off；API 单文件上限默认 200 MiB。`api_storage_policy.max_upload_bytes` 使用同一乐观锁更新，管理员页面以 MiB 输入并由后端强制校验 1–200 MiB，保存后立即影响后续请求，不删除或重处理既有文件。旧数据库里的 `public_pdf_ttl_seconds` 仅作 schema 迁移兼容，不进入公开策略或运行时写入。`state.db` schema 迁移在初始化时进行：v6 用受检 `ALTER TABLE` 为旧库补 `max_upload_bytes` 列和 200 MiB 默认值，v7 重建 `api_display_policy` 为双开关形状，v8 曾加入研究地图渲染枚举，v9 重建该表为四个研究图谱布尔开关（见 §7.7）；迁移均保留旧 policy 的乐观锁版本与审计字段。此后新增的 `tool_error_cards_enabled` 列沿用 v6 `max_upload_bytes` 的受检 `ALTER TABLE` 增量模式，为存量库补默认值 0（不提示），不提升乐观锁版本；`bibtex_export_mode` 列同用该增量模式，为存量库补默认值 `'md_only'`（仅导出 .md），列级 CHECK 限定三枚举值。

hourly cleanup 只在 API 根目录内执行过期、孤立对账和压力清理。75% 清过期并告警；85% 连续清理公共/生成/向量/视觉等可重建数据，不提前删除未过期私有上传；95% 默认 `pause_heavy`（上传、下载、deep_read、OCR、VLM、导出暂停，文字聊天继续），管理员可经预览和二次确认改为 `emergency_evict`；98% 强制暂停文件重任务，不可关闭。in-flight、`protected_until` 和一小时内未完成登记文件受保护。

- **账号数据清理**（`/admin/accounts-data` + `core/admin_accounts.py`）：管理员统一查看 web 账号与 Agent API Key 的数据占用（history_record / data/uploads / trace / API session+private blob），按账号彻底删除。文件删除前全量覆写 + fsync，API state.db 使用 `PRAGMA secure_delete` 并 `VACUUM` 回收空间；旧网络 PDF/assets 由一次性退役迁移清理且不再重建；账号删除只处理该账号拥有的上传、历史、向量与导出。

API Trace 与 web Trace 独立：off 只写匿名请求/错误/Token/耗时聚合；metadata 仅 trace id、模型、工具名、状态、错误码、Token、耗时；full 仅供临时排障并脱敏 Key、bytes、完整正文和 URL query，最长 7 天。web Trace 仍按原路径和原始行为工作。管理员浏览器页面 `/admin/api-storage` 提供策略、API 远程文件上限、容量、状态、清理记录、完整帮助 catalog 和危险操作确认；Agent Key 无管理权限。

### 7.7 一行式状态行、检索表格、图谱附件与展示策略（`/admin/display-policy`）

`tools/export/cards.py` 是 `/v1` 通道专属的卡片渲染层。回显的 assistant content 会作为下一轮 messages 回到模型上下文，因此卡片采用 **context-first 一行式设计**：每个工具结果（`ToolResult.to_dict()` 载荷）只渲染一行状态摘要（emoji 卡头对齐前端 `TOOL_META`，如 `**🔎 文献检索 · 核心集 12 篇 / 候选 13 篇**`、`**📖 上传文件深读 · 2/2 个已解析**`），在工具完成时以 `delta.content` 插入、位于最终回答之前；部分成功加 `🟡` 前缀，缺字段降级为一行摘要，单行 1200 字符上限，渲染永不抛异常。错误状态的 `⚠️` 单行卡由 `tool_error_cards_enabled` 策略单独门控（默认关闭，见下）：关闭时任何错误卡都不进入正式输出与下一轮回显，但错误 ToolMessage 原样交给模型，超时/防重复/预算控制逻辑完全不变——被抑制的只是用户可见展示。

**检索结果规范化表格**：`search_papers` 成功后，通道层用 `render_search_table` 确定性地生成完整论文清单 markdown 表格（列：分层（核心/候选）/标题（截 60 字符、转义管道符）/年份/被引/摘要（有/无）/链接（DOI 优先，无 DOI 取 `urls` 来源页，再无则 —）），与状态行同块插在回答之前。表格是规范化正式输出：由代码保证存在（不依赖模型自觉），不受工具状态行开关控制，仅在 `max_tokens` 派生的 content 预算 < 2000 字符时让位给回答本身；流式路径经 `emit_block(required=True)` 绕过 60% 卡片份额，非流式路径的尾部裁剪只丢弃可选状态行、绝不丢表格块。候选集精简 dict 补带 `doi` 字段以保证链接列数据完整。

策略存于 `data/openai_api/state.db` 的 `api_display_policy` 单行表（schema v9，乐观锁版本并发）。除 `tool_cards_enabled`、`tool_error_cards_enabled`、`skill_card_enabled` 外，研究图谱使用四个严格布尔字段：`research_map_svg_enabled`、`research_map_mermaid_enabled`、`research_map_html_enabled`、`research_map_markdown_enabled`；另有枚举字段 `bibtex_export_mode`（`bib_and_md` / `md_only` / `bib_only`，默认 `md_only`）控制 BibTeX 引用导出的附件格式（清小搭无法下载 `.bib`，`md_only` 导出内容一致的 `.md` 副本并在正式输出末尾附转存说明，见 §7.5；GB/T 7714 与 Web 端不受影响）。`tool_error_cards_enabled` 默认 `false`：错误状态卡（`⚠️` 一行式超时/失败提示）不进入 `delta.content`/`message.content` 与 checkpoint 回显，错误仍完整交给模型决策并由其在回答文字中说明；管理员可在 `/admin/display-policy` 打开以保留原有提示，对成功/部分成功状态卡、检索表格与技能行无影响。后端在合并部分更新后再次校验 SVG / Mermaid / HTML 至少一个为 `true`；Markdown 完全独立。新安装默认仅启用 SVG。v9 迁移保持版本号、更新人和更新时间不变，并按旧策略语义映射：历史兼容输出与“美化 SVG + Markdown”迁为 SVG + Markdown，“美化 SVG”迁为仅 SVG，Mermaid / HTML 均默认关闭。已有 v9 库通过受检 `ALTER TABLE` 增量补 `tool_error_cards_enabled` 列（默认 0）与 `bibtex_export_mode` 列（默认 `'md_only'`），均不动乐观锁版本。运行时不再接受旧枚举。读路径继续使用进程内 5 秒缓存，管理员更新成功后立即失效；读取失败降级到安全默认而不让对话回合失败。管理 API 使用严格 Pydantic、额外字段拒绝与 `expected_version` 乐观锁，前端也阻止关闭最后一种图形输出。

`tools/export/report.py` 先构建共享、确定性的研究图谱视图模型：统一规范主题簇、年份、奠基/候选层、边去重和安全文本，然后由所有格式读取同一模型。每篇论文在所有格式中都保持独立节点，绝不折叠为 "+N" 聚合点。Web 通道仍使用原有 React `GenealogyGraph`，数据结构与交互不变。`/v1` 可同时输出：

- **美化 SVG 附件**：`fileType: image` / `mimeType: image/svg+xml`，纯矢量、自包含，固定 `viewBox`、`width=100%`、`preserveAspectRatio=xMidYMid meet`，无 JavaScript、外部 CSS、远程图片或外部字体；动态文本均 XML 转义。同年同主题的论文以纵向堆叠的独立卡片展示，泳道高度按其最高 (簇, 年) 桶自适应，SVG 总高度随之增长。
- **正文 Mermaid**：确定性的 fenced `mermaid` / `flowchart LR` 块，按主题子图组织，引用边为实线、语义边为虚线，全部论文逐个成节点且不生成 `click` 等可执行指令。它在研究地图状态行之后、最终回答之前进入 assistant content 和下一轮回显；标准客户端不渲染时显示源码。该可选块使用 60% content 预算，预算不足时不截断代码块，而替换为一行省略提示。
- **交互 HTML 附件**：`fileType: text` / `mimeType: text/html`，内联 CSS、JSON 数据和原生 JavaScript，支持平移缩放、主题筛选、引用/语义边切换与节点详情。文档包含严格 CSP，不加载 CDN、字体、图片或第三方脚本，不发起网络请求，也不访问 Cookie、localStorage 或后端 API。JSON 使用安全序列化，动态详情通过 `textContent` 写入。`/files` 将 `.html` 加入 MIME 白名单并始终以 `Content-Disposition: attachment` 下载，主站不内联执行。
- **Markdown 关系说明附件**：`fileType: text` / `mimeType: text/markdown`，包含主题摘要、论文清单、DOI/来源与引用/语义边，不嵌 Mermaid，可独立开启或关闭。

SVG、HTML、Markdown 附件在非流式响应顶层 `x_soda.attachments` 或流式唯一 stop 帧中出现，随后仍只发送一次 `[DONE]`；Mermaid 从不进入附件。所有 API 文件继续走私有 export 生命周期和短期公共别名，字节不写入 Checkpoint。显式 `export_report` 固定输出美化 SVG + Markdown，不读取管理员展示策略，从而保持 Web 端和主动导出的行为稳定。

`scripts/preview_qxd_cards.py` 在本地把全部状态行、检索表格与 SVG 渲染到 `data/preview/`（gitignored）供部署前目检，不参与部署。

### 7.8 其他契约

- 鉴权：Bearer 支持数据库长期 Agent API Key（只存 SHA-256、可撤销）和迁移/应急 `AGENT_API_KEY`（常量时间比较）。本地无配置时接受任意非空 token；生产从未配置任何 Agent Key 返回 503，错误或已撤销 key 返回 401
- 请求验证：`stream` 使用 `StrictBool`；消息非空且至少有一条 user；角色/content part 白名单；`model` 可缺失、空或 null；`max_tokens` 为严格正整数；畸形 JSON 为 400，schema 错误为 422
- `stream` 严格按 JSON 布尔；`max_tokens` 接受并按 chars/4 软截断（截断时 finish_reason=length）；finish_reason 只用官方 5 值
- usage：trace 真实 token 优先，缺失按 chars/4 估算

---

## 8. 检索层（会话级 RAG）

### 8.1 嵌入、向量库与精排（`core/embeddings.py` + `tools/storage/vectorstore.py` + `tools/retrieval/rerank.py`）

- 本地 `paraphrase-multilingual-MiniLM-L12-v2`（~120MB，CPU，离线，中英双语）；加载/编码失败全链路降级。
- ChromaDB 三 collection（cosine）：`summaries`（L1：网络 title+当前有效 abstract；上传可带私有摘要）、`fulltext_chunks`（L2：仅上传全文父子切块，见下）与 `elements`（L3：仅上传图/表/公式的 caption + 结构提取 + VLM 理解文本）。
- **结构化父子切块**（`tools/retrieval/chunking.py` + `vectorstore.build_fulltext_chunks`）：段落/句子边界切块（CJK+拉丁句读，绝不句中截断；单句超长的最后手段硬切），重叠以整句携带；索引单元=~500 字符**子块**（检索精度），每个子块 metadata 带 `parent_text`（~1500 字符父段）+ `parent_id`。
- **cross-encoder 精排**：本地 `BAAI/bge-reranker-base`（~1.1GB，CPU，sentence-transformers 加载，无新依赖）；`storage.reranker_model` 置空即关闭；懒加载单例，下载/加载/打分失败回退 RRF 原序。
- **会话隔离**：L1/L2 记录 metadata 带 `session_id`，id 以 `{session_id}::{paper_id}` 命名空间；全部检索强制 `where session_id=当前会话`，删会话连带删向量。上传文本 chunk 的 paper_id 是 `<attachment_id>`。L3 为跨会话复用视觉成本的全局集合，不带 session_id，但每次查询必须显式过滤为“当前会话上传文档 ID”；因此缓存全局、授权仍是会话级。

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
        → ⑥ 证据缺口检测（摘要没有覆盖时明确返回证据不足，并提示上传原文）
        → ⑦ _validate_citations 防幻觉
```

检索用改写后查询，生成用原始问题；模态检测使用原问题，避免改写丢失“图 3/表格/公式”等信号。向量不可用时 BM25 单独兜底。返回 answer + sources（paper/section/snippet），附件问答还返回理解状态供前端同步。**索引自愈**：文本索引缺失时按当前会话摘要/上传附件补建；指纹命中的元素也会补建全局 element 索引而不触发 VLM。指代解析由系统 prompt 约束：网络论文可传 paper_id 做摘要级限定；全文级问题必须传上传附件 attachment_id，二者互斥且不可混用。

### 8.3 上传论文结构问答

`PaperSummary.section_outline` 与 `document_info` 只由上传论文解析产生，保存有序章节标题、页码范围、解析器、扫描/OCR 和元素统计等轻量元数据，不含绝对路径、原始 bytes 或第二份完整正文。元素理解由 `doc_fingerprint` 命中 SQLite 缓存，避免重复 VLM 消耗。

对上传论文的章节结构、目录和逐节说明查询，会把 `section_outline` 作为受保护 passage 放在生成上下文首位，再用剩余槽位承载 hybrid RAG 结果。网络论文没有结构解析路径，摘要未覆盖的信息必须明确提示证据不足。

OCR 状态严格区分三层：Docling 的数字文本/内置 OCR、仅扫描件触发的 Stage 1.5 VLM-OCR、图/表/公式的 VLM 语义理解。数字文本 PDF 的 `ocr_status=not_needed` 表示正常成功；扫描件为 `recovered` 或 `unavailable_or_empty`。VLM 未配置、熔断或失败始终降级，不使深读硬失败。


---

## 9. 研究地图与谱系图（`agents/map_agent.py` + `tools/storage/genealogy.py`）

### 9.1 研究地图（research_map，LLM 预算 2 次）

1. **主题聚类**：embed(title+abstract) → 凝聚聚类（cosine，distance_threshold 0.5，单簇且论文>6 时自动收紧到 0.4 重试；嵌入不可用回退按子方向分组）；
2. **簇命名与领域脉络**：1 次 LLM 调用（注册 Prompt `map.summary` v3）读入各簇论文（标题+年份+被引数），同时产出每簇 label（id 容错匹配"簇0"→0 + 位置兜底）/overview 与**规范化 landscape**：首句领域整体定位与年份跨度，随后每簇一行「【簇label】（N 篇，起始年–结束年）：概述要点；代表论文：《标题》（年份）。」（代表论文取簇内被引 top 1-3，只允许引用输入中真实出现的标题/年份/数字），末尾 1-2 句簇间演进、分化或交叉；landscape 上限 2000 字符；调用失败回退同格式的确定性文本（簇内被引 top2 作代表论文）；
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
- **谱系图 v2**（`components/GenealogyGraph.tsx` + `lib/genealogy-layout.ts`，纯 SVG 零依赖）：**确定性布局纯函数**（输入确定→输出确定，Node 单测覆盖）：920px 固定内容宽、年份等距刻度（按年份序号而非真实间隔）、泳道按簇大小降序且高度随该簇最高 (簇,年) 桶自适应——每篇论文都是独立节点，绝不折叠为 "+N" 聚合点，同桶按被引降序堆叠、节点半径 3 档；920×560 固定视口 + fit-to-view + 滚轮缩放/拖拽平移/复位。**详情面板**（点节点展开）：元信息+角色徽章+摘要片段、引用关系双列（它引用的/被引用的，仅库内，点击跳转聚焦）、原文链接、**「深问这篇」**（经 `stores/ui.ts` 的 composerDraft 预填聊天输入框，打通图谱→RAG 问答）。**思想源流高亮**：聚焦节点时祖先引用链按年份渐变粗细（越老越粗）。顶部簇筛选 chips/候选集/语义边开关，底部谱系摘要统计行（核心/候选/引用边/语义边/奠基/桥梁）。
- **设计**：「纸墨书院」令牌（`app/globals.css`）：宣纸底 + 墨色 + 黛青主色 + 朱砂点缀，serif 展示标题，tabular-nums 数字，亮/暗双主题（localStorage 持久化），全站语义 token（bg-surface / text-accent 等）统一取色；`/admin/*` 共享左侧分组侧栏布局（`app/admin/layout.tsx`，窄屏转为顶部横向导航），管理页沿用同一令牌配色。
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
- **安全**：SSRF 防护（URL 下载统一走公网校验）、API URL 下载/私有保存默认且最大 200 MiB（管理员可下调）/20 MiB 网页直传上限、UUID/file_id/basename/后缀收敛、图片 raw endpoint 仅开放安全 raster MIME、CORS 显式白名单（永不 `*`）、/v1 Bearer 常量时间比较、Web 附件/导出/上传元素按所有者校验、错误不回显 OS 路径；口令 PBKDF2 加盐哈希、令牌只存哈希、历史按账号隔离（§3.4）。图表裁图与扫描页在按需理解时会发送到配置的第三方 VLM，未配置时优雅降级。

### 部署单 worker 与事件循环隔离

`/v1`、网页 `/api/v1` 和健康检查共享同一个 Uvicorn 事件循环；会话内存、熔断器、LLM 限流信号量也都是进程内状态，因此仍必须 `--workers 1`。同步的 Docling/PyMuPDF 结构解析、SentenceTransformer/CrossEncoder 推理、Chroma 向量读写和附件快速提取不得直接运行在 async 请求链：统一经 `core.blocking.run_cpu_bound` 投递到一个进程级 daemon ML worker 串行执行。SQLite Checkpoint、轻量文件写入等 I/O 经 `core.blocking.run_io_bound` 投递到独立的两个受限 I/O worker，避免 ML 队列形成跨请求队头阻塞。OpenAI-compatible 流式链路验证后先发 role 首帧；工具进度经 reasoning 输出，管理员可调的 `/v1` 软时限（默认 95 秒）停止启动新工作，管理员可调的硬时限（默认 105 秒）前闭合 stop + `[DONE]`（软 ≤ 硬 − 5，经网关转发的请求仍受网关 120 秒限制），客户端断开会取消并回收 producer。原生 ML 调用被取消时无法强杀，但已取消的等待任务不再写入请求状态。扩多 worker/多机前仍须外置共享会话、任务队列、熔断和限流状态，不能直接增加 Uvicorn worker。

前端 `fetchAuthConfig` 使用 8 秒超时；超时只结束首屏无限 spinner，并保留本地已有登录信息。所有 `/api/v1` 权限仍由后端逐请求校验，前端降级不构成授权。

---

## 13. 配置与测试

- `config/settings.yaml`：llm（base_url/model_light/model_reasoning/temperature/timeout，.env `DEEPSEEK_*` 覆盖）、search（每源结果数/源开关）、reader（pdf_dir/parser/read_chunk_chars）、storage（sqlite/chroma/embedding_model）。
- 环境变量：`DEEPSEEK_*`、`MULTIMODAL_*`、`S2_API_KEY`、`OPENALEX_EMAIL`、`CROSSREF_EMAIL`、迁移/应急 `AGENT_API_KEY`、`PUBLIC_BASE_URL`、`AUTH_REQUIRED/REGISTRATION_OPEN/GUEST_ACCESS`、`FRONTEND_ORIGIN/CORS_ORIGINS`、`APP_HOST/APP_PORT`、`BACKEND_URL/NEXT_PUBLIC_BACKEND_URL`、`HF_ENDPOINT/HF_HUB_OFFLINE/XDG_CACHE_HOME`。
- 测试：`./.env_conda/bin/python -m pytest tests/ -q` 默认运行非 slow 回归，普通测试须 stub 外部 LLM/VLM/Docling，不依赖凭据、网络或模型下载；slow 用例通过 `-m slow` 单独运行。`tests/eval/` 提供检索质量黄金集（offline fixture / online 双模式）；前端以 `pnpm build` 作为类型与生产构建闸。真实 API + 浏览器端到端验证属于发布前手动验收，不进入普通 CI。

## 运行时论文渠道与证据边界

论文检索的实时策略存放在 `data/users.db` 的 `paper_search_policy` 单行表中；`config/settings.yaml` 只负责首次种子。管理员通过 `/admin/paper-search` 管理每个平台的 `search` 与 `abstract` 两项能力，5 秒读缓存立即失效，策略使用版本乐观锁。旧数据库中的全文列仅供迁移识别，不进入运行时策略或公开响应。

SearchManager 在创建来源任务前检查静态配置与持久 `search` gate；摘要能力关闭时，来源摘要从会话、向量、地图、问答和综述证据中剥离。网络论文的有效证据边界是当前检索结果中的非空摘要：标题、历史 summary/full_text、PDF URL、旧全文状态和模型常识均不能替代摘要。`deep_read`、`exhibit_index`、`explain_element` 只接受用户上传文件；网络论文遇到全文级问题时返回证据不足并引导上传原文。

上传的 PDF/DOCX/TXT/MD/TEX 通过 sidecar、结构解析、完整文本分块和会话级 RAG 进入全文管线；图表/公式理解只处理上传文件产生的元素。`write_review` 统一准备核心集与候选集全部有效摘要及可解析上传全文，经过证据准备、提纲规划、分主题并行写作、跨主题综合和统一修订，默认中文篇幅按材料量自适应约 3000–6000 字，过短请求提高到覆盖证据所需的最低篇幅。固定输出引言、证据基础、分主题、综合比较、争议/局限/空白、未来方向、结论和证据层级说明；引用先用稳定 ID 校验，再渲染为网络论文落地页或上传文件名。

平台诊断只执行连通、搜索和有效摘要测试。网络 PDF 探测、Unpaywall/doi.org 查询、远程下载、自动全文升级和全文状态展示全部下线。后台启动时运行一次版本标记的 `core.remote_fulltext_retirement` 迁移，删除网络 PDF、网络元素/全文向量与旧全文 summary/status/PDF 路径，清理历史和 API Checkpoint 的网络衍生字段，同时保留上传原件、sidecar、`upload:*` 元素/向量、消息历史和既有导出；CLI 默认 dry-run，可用 `scripts/retire_remote_fulltext_cache.py --execute` 显式执行。

## Runtime performance policy（2026-08）

`runtime_performance_policy` 是 `data/users.db` 中的单行管理员设置，采用严格枚举、5 秒读缓存和乐观锁版本。启动预热模式为 `blocking`（默认，lifespan 就绪前完成本地导入）、`background`（先就绪后后台导入）、`role_first`（清小搭流式请求先返回 `role` 帧，再在线程加载 Agent）和 `off`。预热不调用 LLM/VLM、不下载模型；失败只记录并降级。地图引文模式为 `fast`（批量 OpenAlex 最多 3 秒）、`quality`（最多 8 秒）和 `off`，论文搜索策略关闭 OpenAlex 时有效模式强制为 `off`。

研究地图计算一次标题/摘要嵌入并同时用于聚类和语义边；地图簇标签、概述和领域脉络由注册 Prompt 的一次 utility 调用完成，和引文增强并发。所有增强步骤都可失败，结果仍为 success 并包含 `degraded`、各阶段状态、引文状态、缓存命中和 `stage_ms`。地图缓存指纹包含论文 id/标题/年份/引用数/当前摘要、Prompt 版本和引文策略；新搜索会清空旧地图。

Web 与 `/v1` 都传递 `TurnExecutionContext`。Web 使用 240 秒软时限和 300 秒硬时限；软时限正常收尾并保存，硬时限取消且不保存半轮历史。OpenAI 使用管理员可调的软时限（默认 95 秒）与硬时限（默认 105 秒，约束 软 ≤ 硬 − 5、无固定上限）和既有 SSE 帧序。`search_papers`、`research_map` 每轮最多实际开始一次；参数校验失败不占次数，工具耗尽自身预算和整轮剩余预算使用不同 `timeout_kind`，超时文案明确要求本轮不要再次调用该工具。工具自身预算与整轮预留量不再硬编码，由「运行时工具时限预算」的策略存储管理。
