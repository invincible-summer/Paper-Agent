# OpenAI API Storage Implementation Status

## Active Goal

为 Paper Agent 实现只面向清小搭/OpenAI API 侧的独立单机存储方案：7 天结构化 Checkpoint、独立内容寻址文件库、独立 SQLite/Chroma、管理员可配置的保留与 Trace 策略、磁盘自治清理和 systemd timer；严格保持自制前端用户侧的历史、上传、论文、附件、图谱和持久化保留行为不变，并完成单元、集成、重启恢复、磁盘压力、真实 API 和部署验收。

## Authoritative Files

- Plan: `docs/OPENAI_API_STORAGE_IMPLEMENTATION_PLAN.md`
- Status: `docs/OPENAI_API_STORAGE_IMPLEMENTATION_STATUS.md`
- Repository rules: `AGENTS.md`
- Deployment runbook: `Website_deployment_plan.md`
- Public deployment invariants: `README.md`, `AGENTS.md`, `.env.example`, and `deploy/systemd/`

## Module Checklist

- [x] Module 0 — Baseline, plan persistence, isolation boundary
- [x] Module 1 — StorageContext and API database foundation
- [x] Module 2 — Conversation identity and 7-day checkpoint
- [x] Module 3 — API artifact store, PDF, multimodal, Chroma isolation
- [x] Module 4 — Retention cleanup and disk pressure policy
- [x] Module 5 — API trace policy
- [x] Module 6 — Administrator APIs and storage UI
- [x] Module 7 — systemd, deployment, operations documentation
- [x] Module 8 — Full tests, real API, restart and pressure acceptance

## Current Module

Modules 0–8 completed on August 14, 2026. Requirement-by-requirement completion audit passed. API storage schema version: `3`.

## Fixed Decisions

- Changes apply only to `/v1`; self-hosted frontend persistence remains unchanged.
- API root: `data/openai_api/`.
- Default session/upload TTL: 7 days.
- Default export TTL: 24 hours.
- Public PDF TTL is 3 days: scheduled cleanup deletes expired copies, and the next deep_read automatically re-downloads and re-extracts the paper.
- Default semantic/vision cache TTL: 90 days.
- Default API trace mode: off.
- Disk thresholds: 75/85/95/98 percent.
- 85% policy: continuous eviction of old rebuildable API data.
- 95% default: pause heavy file tasks; administrator may choose emergency eviction.
- 98%: mandatory file-write protection.
- Every administrator policy requires a detailed explanation modal.
- The user cancelled the earlier per-module `/compact` requirement on August 14, 2026; the complete Goal, module sequence, acceptance gates and isolation boundary remained unchanged.

## Web Persistence Invariants

The API storage implementation does not alter or clean:

- `/api/v1/*` behavior;
- `history_record/chat_*.json`;
- web uploads, PDFs, assets, exports or Chroma data;
- browser accounts, browser tokens, administrator identity or Agent API Key lifecycle.

The API cleanup root is containment-checked and restricted to `data/openai_api/`. Emergency eviction, legacy scan and administrator cleanup cannot cross into web storage.

## Implemented Architecture

- `core/storage_context.py` and `core/api_storage_store.py` provide channel-aware paths and an API-only SQLite database using WAL, foreign keys, schema migrations, restrictive permissions and parameterized SQL.
- `core/api_checkpoint.py` stores a bounded zlib JSON structural checkpoint instead of complete message transcripts. It restores topic, paper set, read summaries, maps and RAG state, uses HMAC message-chain aliases, rejects ambiguous aliases and quarantines corrupt checkpoints.
- `core/api_artifact_store.py` provides private session-HMAC uploads, public PDF SHA-256 deduplication, content-addressed blobs, unique public aliases, streaming URL ingest with redirect-by-redirect SSRF checks and API-only metadata/Chroma/vision caches.
- `core/api_storage_cleanup.py`, `core/storage_pressure.py` and `scripts/cleanup_openai_api_storage.py` implement expiry, reconcile, one-time preview tokens, in-flight/protected-file guards, pressure admission and safe cleanup.
- API Trace supports `off`, `metadata` and redacted `full`; web Trace retains its prior path and behavior. API Trace retention is capped at 7 days.
- Administrator APIs and `/admin/api-storage` expose policy, usage, status, cleanup history, preview/execute and legacy scan with browser-admin authorization, optimistic version conflicts, help modals and dangerous-action confirmation.
- `deploy/systemd/paper-agent-cleanup.service` and `.timer` run low-priority hourly cleanup with `Persistent=true`, `OnBootSec=10min`, `OnUnitActiveSec=1h` and `RandomizedDelaySec=5min`; the only writable path is the API storage root.

## Module 8 Final Acceptance Evidence

### Automated regression

- Full backend suite: `573 passed, 3 deselected, 1 warning in 55.78s`.
- The only backend warning was the existing PyTorch CUDA-driver warning.
- Frontend `pnpm lint`: passed; only the existing non-blocking `@next/next/no-img-element` warning remained.
- Frontend `pnpm build`: passed and generated `/chat`, `/login`, `/admin/agent-keys` and `/admin/api-storage`.
- Storage/deployment/systemd focused tests passed.
- `git diff --check` passed.

### Real OpenAI-compatible LLM stream

A real `/v1/chat/completions` request completed with the required protocol sequence: first role frame, streamed `delta.reasoning`, streamed `delta.content`, one `finish_reason=stop` frame with usage, and terminal `[DONE]`.

```text
frames=52
reasoning_chars=246
content_chars=69
finish=stop
```

The answer correctly explained “文献综述”.

### Real retrieval, deep read and restart restoration

- A real `/v1` conversation searched `retrieval augmented generation survey`, retained 12 core papers and deeply read one paper.
- The stored structural checkpoint contained the topic, 12-paper set and one read summary without storing full messages or reasoning.
- After stopping and restarting the backend, continuing with the original client-supplied complete message list restored the research state and produced:

```text
研究主题：retrieval augmented generation survey
核心论文：12
已深读：1
已深读论文：
Retrieval-Augmented Generation for Large Language Models: A Survey
```

- `ChatSession.context_summary()` now carries the stable paper-id catalogue for both core and candidate papers across all channels, so the model can directly call deep_read/ask_papers on a named paper instead of re-running search_papers; entries also carry the verified fulltext status (全文✓/仅摘要/待验证). API channel additionally lists up to five deeply-read titles.

### Real self-hosted frontend and multimodal acceptance

- A real Next.js → FastAPI → configured LLM browser run returned `FRONTEND_E2E_OK`; the thinking fold rendered and the browser made exactly one chat request.
- A PNG and DOCX were uploaded through the real frontend. Both reached `multimodal_status=ready` with one indexed element, then succeeded through `deep_read` and `ask_papers`.
- The real answer found `DOCX_MULTIMODAL_E2E_20260814` and explained the INPUT → OUTPUT image flow.
- This verifies that the API-only storage changes did not replace the existing web upload, multimodal, history or RAG paths.

### Real Unicode DOCX download

The real frontend requested manuscript export and downloaded:

```text
20260814_132722_堂吉诃德的理性与非理性_韦伯卡里斯马理论视角_论文提纲.docx
```

The `/files/<Unicode filename>.docx` response was `200 OK`, contained 38,791 bytes and began with the ZIP `PK` signature. This directly covers the former long-Chinese DOCX `400 Bad Request` failure.

### Administrator, cleanup and pressure acceptance

- Real administrator browser E2E opened `/admin/api-storage`, displayed the detailed policy help modal, required dangerous confirmation when shortening privacy TTL, previewed and executed immediate cleanup, and recorded `execute:immediate_cleanup`.
- CLI cleanup preview and single-use execution succeeded: `cleanup_cli_preview_execute=ok`.
- Simulated pressure covered 74%, 76%, 86%, 96% pause-heavy, 96% emergency, 98% and recovery. Text chat remained available; heavy writes followed policy; in-flight and newly protected files were preserved.
- Automated isolation tests verified that pressure cleanup and emergency eviction leave web files and history unchanged.

### Trace, secrets, runtime and process audit

- With default API Trace off: `api_trace_files=0`, `trace_records=0`; only anonymous aggregates were present.
- The API checkpoint/trace audit found neither the local test token nor reasoning content.
- No project `data/openai_api` runtime tree remained after acceptance cleanup.
- Temporary E2E roots, generated test history, attachments, sidecars, element assets, exported DOCX, test metadata rows and test vectors were removed.
- No Goal-started pytest, Uvicorn or Next process remained.
- Tracked secret-pattern filenames contained only `.env.example`; the new-source secret scan passed. No real API key was added to code, docs, fixtures or screenshots.
- Existing pre-Goal runtime Chroma modifications were not reset, rewritten or claimed as this Goal’s isolated output.

## Deployment Acceptance

- `Website_deployment_plan.md`, `README.md`, `docs/DESIGN.md`, `AGENTS.md`, `.env.example` and `deploy/systemd/` document the API-only lifecycle and operations model.
- API-only 2 vCPU / 4 GiB is documented as a constrained lightweight deployment: one Uvicorn worker, one heavy research/read task at a time, swap enabled, and no self-hosted Next.js requirement.
- The supported 3–6 mixed-concurrent-user deployment remains 4 vCPU / 16 GiB, 100 GiB storage, 10 Mbps and 4 GiB swap.
- API blobs, temporary files, traces and expired checkpoints are excluded from long-lived backups by default; any checkpoint backup must not extend data beyond its business TTL.
- Administrator bootstrap, one-time Agent API key issuance, revocation, cleanup timer installation, monitoring, rollback and recovery commands are documented without embedding credentials.

## Baseline Evidence

Before API storage implementation, the baseline was:

- Backend: `490 passed, 3 deselected, 1 warning in 42.59s`.
- Frontend lint/build passed with the same existing non-blocking image warning.
- `/v1` used in-process `SessionMemory` with a 2-hour TTL and 200-session LRU.
- Existing web persistence used `history_record/`, `data/uploads`, `data/pdfs`, `data/assets`, `data/exports` and the legacy web Chroma paths.

## Risks / Operational Boundaries

- The repository still requires one Uvicorn worker because session memory, circuit breakers and semaphores are process-local.
- 2 vCPU / 4 GiB is viable only for API-only light use with heavy-task concurrency constrained to one; it is not the supported 3–6 mixed-concurrency tier.
- The worktree contains substantial pre-existing and cross-feature changes, including runtime Chroma modifications; they must not be reset merely to clean this Goal’s diff.
- OpenAI compatibility carries text/reasoning plus `x_soda.attachments`; it does not carry the self-hosted React tool cards or interactive `GenealogyGraph`. Portable Markdown/SVG output remains the compatibility path.

## Next Action

All modules and available acceptance gates are complete. Perform the final format/document check, then mark the active Goal complete.
