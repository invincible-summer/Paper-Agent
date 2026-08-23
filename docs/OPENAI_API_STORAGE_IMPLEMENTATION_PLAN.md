# 清小搭 / OpenAI API 侧独立存储实施计划

> 执行协议：同一个 Goal 从模块 0 持续到模块 8；每完成一个大模块，必须先验证、更新状态文件，然后立即执行 `/compact`，压缩后重新读取本文件与状态文件，再进入下一模块。不得等整个 Goal 完成后才 compact。

> 执行覆盖（2026-08-14）：用户明确取消后续 `/compact`，要求在完成每个模块的验证和状态落盘后直接继续下一模块。完整 Goal、模块顺序、验收门槛和隔离边界保持不变。

## 1. 总目标与隔离边界

Goal：为 Paper Agent 实现只面向清小搭/OpenAI API 侧的独立单机存储方案：7 天结构化 Checkpoint、独立内容寻址文件库、独立 SQLite/Chroma、管理员可配置的保留与 Trace 策略、磁盘自治清理和 systemd timer；严格保持自制前端用户侧的历史、上传、论文、附件、图谱和持久化保留行为不变，并完成单元、集成、重启恢复、磁盘压力、真实 API 和部署验收。

只改造：

```text
/v1/models
/v1/chat/completions
清小搭 URL/多模态文件输入
清小搭研究、深读和 RAG 状态
清小搭导出附件
清小搭 API Trace
清小搭 API 数据生命周期
```

不得改变：

```text
/api/v1/*
history_record/chat_*.json
自制前端账号历史、上传、下载、研究地图、交互图谱和 Chroma 会话
自制前端用户主动删除历史的语义
data/users.db 中的账号、浏览器令牌、管理员和 Agent API Key 生命周期
```

API 新根目录：

```text
data/openai_api/
├── state.db
├── metadata.db
├── chroma/
├── blobs/
├── tmp/
└── traces/
```

API 清理器只能操作 `data/openai_api/`。即使管理员启用紧急删除，也不得扫描、迁移或删除 web 目录。

## 2. 每模块 `/compact` 强制协议

权威状态文件：`docs/OPENAI_API_STORAGE_IMPLEMENTATION_STATUS.md`。

每个模块完成条件：实现完成、迁移完成、聚焦测试通过、相关回归通过、文档同步、`git diff --check` 通过、无秘密或运行时产物进入 Git、无本模块测试/服务仍在运行、状态文件已更新、下一模块入口已记录。

严格顺序：

```text
完成模块 → 测试 → 更新状态 → 输出检查点 → /compact
→ 重读本计划、状态文件和 AGENTS.md → 确认同一 Goal active → 下一模块
```

每次 compact 检查点必须保留：Goal 原文、计划/状态路径、模块列表、已完成/未完成模块、schema 版本、关键决策、修改文件、测试结果、风险、下一模块及首个动作、web 不受影响约束、API 根目录、默认策略。

若客户端不能由 Codex 主动运行 `/compact`，必须停在模块边界并让用户执行，不得假装已压缩。

模块顺序：

| 模块 | 内容 | 完成后 |
|---|---|---|
| 0 | 基线、计划落盘、隔离边界 | `/compact` |
| 1 | StorageContext 与 API 数据库 | `/compact` |
| 2 | 会话识别与 7 天 Checkpoint | `/compact` |
| 3 | API 文件库、PDF、多模态、Chroma 隔离 | `/compact` |
| 4 | 生命周期清理与磁盘压力 | `/compact` |
| 5 | API Trace 策略 | `/compact` |
| 6 | 管理员 API 和存储管理页面 | `/compact` |
| 7 | systemd、部署和运维文档 | `/compact` |
| 8 | 完整测试、真实 API、重启和压力验收 | `/compact` 后再 Goal completion audit |

## 3. 模块 0：基线与计划落盘

- 落盘本计划与状态文件。
- 记录 `/v1` 当前 2 小时内存 SessionMemory、ChatSession 字段、文件/SQLite/Chroma/Trace 路径和管理员接口。
- 记录 web 持久化不可变回归清单。
- 运行完整非慢速后端、前端 lint/build、`git diff --check`。
- 不修改或清理已有运行时 Chroma、历史和用户文件。
- 状态文件记录基线、工作区、下一模块入口。

## 4. 模块 1：StorageContext 与 API 数据库基础

新增：

```python
@dataclass(frozen=True)
class StorageContext:
    channel: Literal["web", "openai_api"]
    workspace_id: str
    root_dir: Path
    state_db: Path
    metadata_db: Path
    chroma_dir: Path
    blob_dir: Path
    temp_dir: Path
    trace_dir: Path
```

- `StorageContext.web()` 返回现有目录；`openai_api()` 返回 `data/openai_api`。
- `ChatSession` 增加 `channel="web"` 和内部 storage context；web 默认行为不变。
- 历史/Checkpoint 不保存绝对路径。
- API 目录 700、数据库/私有文件 600。
- `.env.example` 增加无秘密 bootstrap：`OPENAI_API_STORAGE_ROOT`、`OPENAI_API_POLICY_PRESET`；数据库策略初始化后优先。

`state.db` 使用 WAL、外键、参数 SQL和迁移版本，建立：

- `api_storage_policy`：TTL、阈值、pressure/critical/trace 策略、版本和修改人。
- `api_sessions`：credential、RAG session、压缩 checkpoint、访问/过期、版本和状态。
- `api_session_aliases`：lookup hash、session、过期、ambiguous。
- `api_artifacts`：hash、作用域、类别、隐私级、逻辑/公开名、MIME、相对路径、容量、TTL、保护和状态。
- `api_session_artifacts`：会话与 artifact 引用。
- `api_cleanup_runs`：清理审计。
- `api_cleanup_previews`：10 分钟一次性确认 token。
- `api_runtime_state`：重任务暂停、原因、磁盘占用和最后清理。

默认：session/upload 7d、export 24h、legacy network-PDF retention retired（不再自动重下；用户明确提供的上传文件按私有上传生命周期处理）、cache 90d、trace off、clean 60min、75/85/95/98、continuous_evict、pause_heavy。

测试：建库/迁移/WAL/外键/策略版本、web/API 路径隔离、路径逃逸与 symlink 拒绝、API 策略不能读取 web 历史。

## 5. 模块 2：会话识别与 7 天 Checkpoint

Agent Key 内部验证返回 credential principal（key_id/created_by/source），不返回或记录明文 Key。

会话查找：

1. 取得 credential ID 和可选 OpenAI `user`；
2. 排除当前最后 user 消息；
3. 规范化此前消息链；
4. `HMAC(server_secret, credential_id + optional_user + canonical_prior_messages)`；
5. 唯一 alias 命中时恢复；未命中、过期、冲突或 ambiguous 时新建；不猜测。

HMAC secret 首次随机生成、保存 state.db、权限 600、无环境变量人工管理。

正常结束前，根据“本轮输入 messages + 最终 assistant content”建立下一轮 alias；reasoning 不参与；异常断流不建立最终 alias。一个 alias 指向不同 session 时标记 ambiguous，未来一律新建，避免串会话。

Checkpoint 保存：topic/conception/language/field_profile、papers/candidates/summaries、map/path/review、sub-directions/search queries、附件引用、loaded skills、RAG session。明确排除完整 messages、reasoning、思维链、Key、文件 bytes、完整 PDF 正文、完整 Trace 和任何网络全文状态。

格式：版本化 JSON + zlib，无 pickle，未压缩上限 8 MiB；单个大字段超过 256 KiB 时外置为 private/generated `state_payload` artifact。

orchestrator 增加可选 `checkpoint_cb`，成功工具批次、Skill 状态变化、重要 session 变更、done/max-iterations 前写入；web 不传 callback。API session 使用 asyncio lock + SQLite optimistic version。

测试：跨 Store/重启恢复、TTL、不落 messages/reasoning、user 有无、同首句分叉、alias ambiguous、中断流、大字段外置、并发版本冲突、损坏 schema 降级。

## 6. 模块 3：API 文件库、PDF、多模态和向量隔离

类别：upload、upload_sidecar、legacy_public_pdf（仅迁移兼容，不得新增运行时路径）、element_asset、export、state_payload、temporary。

- 网络论文公开 PDF 不再创建或复用；仅保留历史数据清理迁移。
- 私有上传用 `HMAC(secret, session_id + SHA256(bytes))`，不跨私有 session 自动共享。
- 导出产物使用唯一公开 alias，展示名与物理名分离，默认 24h。
- URL 文件流式写 tmp，边下载边 hash，继续 SSRF/公网 IP/重定向/MIME/50 MiB 限制；不把 50 MiB 全放内存；成功 atomic rename，失败清 tmp；摄取阶段零 VLM。
- 上传、用户文件解析器、Docling assets、OCR/VLM、metadata、Chroma、报告/文稿导出、`/files` 和 element asset 全部按 StorageContext；无 context 时继续 web 旧路径。
- API 使用独立 metadata.db/chroma；清 API session 不影响 web；私有元素只在所属 session 检索。
- `/files/{filename}` 保持协议：先解析 API public alias，未命中再查 web export；basename/MIME/Unicode 保持；不暴露物理 hash 路径；API 过期 404，清理不删 web。

测试：网络论文全文/PDF 路径不存在；显式用户文件上传隔离、私有隔离、用户附件流式下载和中断、API/web Chroma/metadata/文件隔离、PDF/DOCX/PNG/VLM 降级、Unicode DOCX PK、MD/TXT/TEX/SVG、web 旧文件回归。

## 7. 模块 4：生命周期和磁盘自治

新增 `scripts/cleanup_openai_api_storage.py`，支持 `--scheduled`、`--preview`、`--execute-token`、`--reconcile`。

正常顺序：过期 preview、tmp、exports、API traces、aliases、checkpoints、API Chroma session、引用、零引用 private、legacy public-PDF、assets、孤立对账。删除幂等；数据库/文件中断可 reconcile。

保护：in-flight session、下载/OCR/VLM/export 中 artifact、protected_until、最近 1h 未完成登记文件。

阈值：

- 75%：告警、删过期。
- 85%：continuous eviction：过期、tmp、旧版遗留公共缓存/资产/向量/视觉缓存；不提前删未过期私有上传，也不创建新的网络论文 PDF。
- 95% 管理员可选：
  - 默认 pause_heavy：暂停上传、下载、deep_read、OCR、VLM 文件理解和导出；普通文字问答继续。
  - emergency_evict：先删未过期公共缓存和生成物，再删无 in-flight、24h 未访问的非活跃私有上传；标记 evicted。旧网络文件不得重下，私有上传丢失时要求用户重传。
- 98%：不可关闭的最终保护，强制暂停文件写入型重任务，文本问答继续，空间恢复自动解除。

统一 `StoragePressureGuard.ensure_allowed(operation, context)`，新增 `ErrorCode.STORAGE_PRESSURE`。操作：upload/download/deep_read/ocr/vision/export/text_chat。

测试：所有阈值、两种 95% 策略、98%、保护、eviction、文本问答、自动恢复、清理幂等、preview token、绝不触碰 web。

## 8. 模块 5：API Trace 策略

web Trace 现状不变；API Trace 独立且默认 off。

- off：不落逐请求 Trace，只留匿名聚合请求/错误/Token/耗时/错误码。
- metadata：trace_id、模型、工具名、状态、错误码、Token、耗时；无用户原文、参数、reasoning、文件名/URL。
- full：临时排障可存问题和参数，但 Key、文件 bytes、完整论文正文永不记录；URL query/敏感字段脱敏；最长 7d；UI 警告并支持自动关闭。

Trace 增加 channel/policy，保持现有 web 调用兼容。

测试：默认无 API trace、metadata 无原文、full 脱敏、web 不变、热切换、TTL、Key/bytes 不出现。

## 9. 模块 6：管理员 API 和页面

新增 `/admin/api-storage`。

接口：

```text
GET/PUT /api/v1/admin/api-storage/policy
GET     /api/v1/admin/api-storage/usage
GET     /api/v1/admin/api-storage/status
GET     /api/v1/admin/api-storage/cleanup-runs
POST    /api/v1/admin/api-storage/cleanup/preview
POST    /api/v1/admin/api-storage/cleanup/execute
POST    /api/v1/admin/api-storage/legacy-scan
```

只接受管理员浏览器 token，Agent Key 不可管理。版本冲突 409。

预设：

- privacy：session/upload 2h、export 2h、PDF 7d、cache 30d、trace off。
- balanced 默认：7d、24h、30d、90d、trace off。
- performance：30d、7d、90d、180d、metadata 7d。
- custom：在安全范围内自定义。

每个选项必须有详细解释弹窗：功能、涉及数据、优缺点、隐私、磁盘、延迟、费用、连续性、重检/OCR/VLM、生效时间、紧急行为、恢复默认。覆盖所有预设、TTL、清理间隔、75/85/95/98、continuous eviction、pause/emergency、off/metadata/full、立即清理、遗留扫描。

缩短 TTL、full Trace、紧急私有删除、立即清理和影响现有数据的 custom 必须 preview + 二次确认。preview token 10min/单次/绑定策略版本。页面显示分类容量、普通问答/重任务状态、清理记录，不显示用户原文、Key、物理绝对路径。帮助 catalog 由后端版本化返回。

测试：admin/403、Pydantic 范围/交叉验证、409、preview/execute、帮助 catalog 覆盖、危险确认、lint/build、Playwright、无敏感数据。

## 10. 模块 7：systemd、部署和文档

新增 oneshot cleanup service + timer：OnBootSec=10min、OnUnitActiveSec=1h、Persistent=true、RandomizedDelaySec=5min，只允许写 `/opt/paper-agent/data/openai_api`，低 IO/CPU 优先级。

目录 700、DB/私有文件 600。默认备份 users.db/策略/配置，不长期备份 API private/tmp/traces/exports/过期 checkpoint；若备份 checkpoint，文档警告备份保留期不得突破业务 TTL。

更新 README、DESIGN、Website_deployment_plan、.env.example、AGENTS。明确 API-only、web 不变、7d、trace off、管理员策略、2C4G 限制、timer、磁盘恢复、隔离。原则上无新依赖；若实现新增依赖，必须同步 requirements/lock/部署手册。

测试：unit/timer dry-run、权限、服务账号、日志无原文/Key、手册可从空服务器执行、.env.example 仅占位。

## 11. 模块 8：最终回归与真实验收

自动化：全后端、前端 lint/build、diff check。专项覆盖 storage context、checkpoint、artifact、cleanup、pressure、trace、admin policy、OpenAI storage integration。

重启真实验收：`/v1` 检索/深读 → 停后端 → 重启 → 用清小搭风格完整 messages 继续 → 恢复论文、摘要、RAG；数据库无完整 messages/reasoning。

文件/多模态：URL PDF、PNG/JPG/WebP、DOCX、OCR、VLM、降级、Unicode DOCX PK、MD/TXT/TEX/SVG、x_soda、导出/会话到期模拟。

压力：74/76/86/96 pause/96 emergency/98/恢复，确认 web 文件和历史完全不变。

管理员真实 E2E：策略、解释弹窗、Trace、两种 95%、preview、确认、用量、清理历史、普通用户 403、无敏感信息。

模块 8 完成后仍先更新状态并 `/compact`；压缩后重读完整计划，逐项 completion audit，证据完整后才 `update_goal complete`。

## 12. 最终默认决策

- 单台云服务器本地 ESSD；不依赖个人电脑、OSS、Redis、PostgreSQL 或外部向量库。
- 只改 `/v1`；web 持久化不变。
- API 独立 `data/openai_api`。
- Checkpoint/upload 7d、export 24h、legacy network-PDF retention retired（不再清理后重下，deep_read 只解析当前会话上传文件）、cache 90d。
- 不重复保存完整 messages，不保存 reasoning/思维链。
- Trace 默认 off。
- 85% continuous eviction。
- 95% 默认 pause_heavy，可选 emergency_evict。
- 98% 不可关闭保护。
- 管理员所有策略必须有完整解释弹窗。
- 每完成一个大模块立即 `/compact`；Goal 从模块 0 持续到最终审计。
