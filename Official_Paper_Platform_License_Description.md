# 论文平台官方说明与许可规范（Paper Agent 合规文档）

> 本文档完整列出 Paper Agent 使用的全部论文数据平台：每个平台是什么、Agent 如何使用它、其许可/条款在哪里（附官方链接）、以及我们的合规措施。
> 原则：**只使用官方公开的免费 API；元数据按各平台许可使用；全文只获取开放获取（OA）版本；绝不抓取付费墙、绝不使用违反 ToS 的渠道。**

## 总览表

| 平台 | 角色 | 认证方式 | 许可 | 条款链接 |
|------|------|----------|------|----------|
| OpenAlex | 检索 + 引用边 | 可选邮箱（polite pool） | 数据 CC0 | https://docs.openalex.org/ |
| Semantic Scholar | 检索 | 可无 key；S2_API_KEY 更稳 | 免费学术 API | https://www.semanticscholar.org/product/api |
| arXiv | 检索 + OA 全文 | 无需认证 | 元数据 CC0 | https://info.arxiv.org/help/api/tou.html |
| Crossref | 检索（全学科 DOI 元数据） | 可选邮箱（polite pool） | 元数据 CC0/CC-BY | https://www.crossref.org/documentation/retrieve-metadata/rest-api/ |
| Europe PMC | 检索 + OA 全文（生物医学） | 无需认证 | 免费 REST；全文按各篇许可 | https://europepmc.org/RestfulWebService |
| DOAJ | 检索（OA 期刊，人文社科强） | 无需认证 | 元数据 CC BY-SA | https://doaj.org/api/v4/docs |
| HAL | 检索 + OA 全文（人文社科/学位论文） | 无需认证 | 免费 API（CCSD） | https://api.archives-ouvertes.fr/search/ |
| OpenAIRE | 检索 + OA 链接（欧盟 OA 图谱） | 无需认证 | 数据 CC-BY | https://graph.openaire.eu/develop/ |
| CORE | 检索 + OA 全文（机构库聚合） | CORE_API_KEY（免费注册）；无 key 自动停用 | 免费层 + T&C | https://core.ac.uk/services/api |
| Unpaywall | OA 全文定位（DOI 查询） | 必须邮箱参数；无邮箱则不调用 | 免费 API（ToS 见下） | https://unpaywall.org/legal/terms-of-service |

---

## 1. OpenAlex — https://openalex.org

- **是什么**：OurResearch 出品的全学科开放学术图谱（2.5 亿+ 论文元数据：标题/摘要/作者/年份/期刊/被引/DOI/开放获取状态）。
- **Agent 如何使用**：
  - 关键词检索：`GET https://api.openalex.org/works?search=...`（`tools/search/openalex.py`）
  - 谱系图真实引用边：按 DOI 查 `referenced_works`（`tools/search/openalex_refs.py`）
  - 全文候选：仅取响应中的 `primary_location.pdf_url` / `open_access.oa_url`（平台官方标注的 OA 链接）
- **许可**：数据 **CC0**（公有领域），可自由使用，含商用；API 免费。
- **合规措施**：配置真实邮箱时带 `mailto` 进 polite pool（更宽松）；**未配置邮箱时匿名访问标准池，绝不发送占位/虚假身份**（本轮已修正）。限流 5 并发 / 0.5s 间隔。

## 2. Semantic Scholar — https://www.semanticscholar.org

- **是什么**：Allen Institute for AI 的学术图谱 API（2.14 亿论文，含 `openAccessPdf` 字段）。
- **Agent 如何使用**：关键词检索 `GET https://api.semanticscholar.org/graph/v1/paper/search`（`tools/search/semantic_scholar.py`），取 `openAccessPdf.url` 作为 OA 全文候选。
- **许可**：免费学术 API。官方说明：多数端点无需认证（未认证用户共享全局限流）；申请免费 API key 可获得独立配额（入门 1 RPS）。
- **合规措施**：配置了 `S2_API_KEY` 时带 `x-api-key`；未配置则以公开匿名方式低频使用（1.5s 间隔 + 429 退避）。限流 3 并发 / 1.5s。
- 官方说明：<https://www.semanticscholar.org/product/api>

## 3. arXiv — https://arxiv.org

- **是什么**：康奈尔大学运营的预印本平台（CS/物理/数学/定量生物/经济学），作者自存档。
- **Agent 如何使用**：关键词检索 `GET https://export.arxiv.org/api/query`（`tools/search/arxiv.py`）；全文取结果中的 PDF 链接，服务端解析用于问答，**不对外提供 PDF 副本**。
- **许可**（[ToU 原文](https://info.arxiv.org/help/api/tou.html)，已逐条核对）：
  - 元数据 **CC0**，可自由检索/存储/转换/分享；
  - 明确鼓励"构建帮助用户发现 e-print 的工具与服务"（更好的搜索界面、引用图谱等——正是本 Agent 的形态）；
  - **禁止"在自己服务器存储并对外提供 e-print（PDF）"**——我们不设 PDF 下载端点，`/files` 只托管 Agent 生成的 .md 报告（代码已核实）；
  - 硬性限流："**每 3 秒至多 1 请求、单连接**"。
- **合规措施**：限流器已按其 ToU 调整为 **1 并发 / 3.0s**（`tools/search/arxiv.py:14`）；尊重 robots 与连接数限制。

## 4. Crossref — https://www.crossref.org

- **是什么**：DOI 注册机构的官方元数据 API，全学科覆盖（人文/社科/医学的 DOI 元数据基本盘）。
- **Agent 如何使用**：关键词检索 `GET https://api.crossref.org/works?query=...`（`tools/search/crossref.py`）。仅元数据；其 `link` 字段不作为全文来源（可能指向付费墙）。
- **许可**：公共 REST API 免费；元数据多为 CC0/CC-BY（因成员出版社而异）。建议带 `mailto` 进 polite pool。
- **合规措施**：同 OpenAlex 的邮箱策略——有真实邮箱则带，无则匿名，绝不发占位身份。限流 5 并发 / 0.5s。

## 5. Europe PMC — https://europepmc.org

- **是什么**：欧洲生物信息研究所（EMBL-EBI）的生命科学文献库，含 PubMed 内容 + 预印本（bioRxiv/medRxiv 已索引）+ OA 全文。
- **Agent 如何使用**：关键词检索 `GET https://www.ebi.ac.uk/europepmc/webservices/rest/search`（`tools/search/europepmc.py`）。全文链接**只收 `availabilityCode=OA` / "Open access" 的 PDF**；订阅（subscription）链接在解析层就被丢弃（测试锁定：`tests/test_pdf_fetcher.py::test_europepmc_oa_only`）。
- **许可**：REST API 免费公开；全文内容按各篇论文自身许可（OA 子集为 CC 系列）。
- 官方说明：<https://europepmc.org/RestfulWebService>

## 6. DOAJ — https://doaj.org

- **是什么**：开放获取期刊目录（瑞典 Lund 大学基础设施），收录的全部是 OA 期刊，人文社科尤其强。
- **Agent 如何使用**：关键词检索 `GET https://doaj.org/api/search/articles/{query}`（`tools/search/doaj.py`）；全文取 `bibjson.link` 中的 fulltext 链接（DOAJ 收录即 OA）。
- **许可**：API 免费公开；**元数据 CC BY-SA**（署名 + 相同方式共享）。
- **合规措施**：署名义务已在产品设置弹窗"数据来源"中履行（见 §署名）。限流 2 并发 / 1.0s。
- 官方说明：<https://doaj.org/api/v4/docs>

## 7. HAL — https://hal.science

- **是什么**：法国国家开放档案库（CCSD 运营），作者自存档的 OA 知识库，人文社科、欧洲学位论文覆盖强。
- **Agent 如何使用**：关键词检索 `GET https://api.archives-ouvertes.fr/search/`（`tools/search/hal.py`）；全文取 `fileMain_s`（HAL 托管的作者自存档 PDF，天然 OA）。
- **许可**：免费开放 API；内容为作者按 HAL 协议自存档的开放副本。
- 官方说明：<https://api.archives-ouvertes.fr/search/>

## 8. OpenAIRE — https://www.openaire.eu

- **是什么**：欧盟委员会支持的 OA 研究图谱，聚合全球仓储/出版社的 OA 记录。
- **Agent 如何使用**：关键词检索 `GET https://api.openaire.eu/search/publications?format=json&keywords=...`（`tools/search/openaire.py`）。全文链接**只取 `accessright` 明确标记为 Open Access 的实例**（closed/restricted/embargo 一律不取，测试锁定）。
- **许可**：数据 **CC-BY**（需署名）；API 免费。
- 官方说明：<https://graph.openaire.eu/develop/>

## 9. CORE — https://core.ac.uk

- **是什么**：全球最大的机构知识库聚合（开放大学运营），4.5 亿+ 可检索记录、5700 万+ 全文。
- **Agent 如何使用**：关键词检索 `GET https://api.core.ac.uk/v3/search/works`（`tools/search/core.py`），全文取 `downloadUrl`（CORE 托管的仓储 OA 副本）。
- **许可**（[官方页原文](https://core.ac.uk/services/api)，已核对）：API 免费层可用；"可以商用（适用其 T&C）"；机构/企业高速率通常需许可评估。
- **合规措施**：**未配置 `CORE_API_KEY` 时该源整体自动跳过，不发起任何请求**；免费申请 key 后启用。限流 3 并发 / 0.5s。

## 10. Unpaywall — https://unpaywall.org

- **是什么**：OurResearch 的 OA 定位数据库——按 DOI 查询某论文在全球的合法 OA 副本位置（出版社金色 OA / PMC / 机构库）。
- **Agent 如何使用**：全文获取的最后兜底：`GET https://api.unpaywall.org/v2/{doi}?email=...`（`tools/pdf/fetcher.py::_unpaywall_pdf_url`）。只接受 `is_oa=true` 的响应，取 `best_oa_location.url_for_pdf`。
- **许可**（[ToS 原文](https://unpaywall.org/legal/terms-of-service)，已逐条核对，2020-11-05 版）：
  - "We provide most features of the Service **free of charge**, and you generally **do not need to register**"——API 属免费功能；
  - API 本身就是为自动化查询提供的组件，本 Agent 的逐 DOI 查询属于其明示用法；
  - 收费的是 Data Feed（每周全量变更文件订阅），我们不使用；
  - 禁止"未经授权复制/再分发 Database"——我们只做单篇实时查询，不建镜像库，符合。
- **合规措施**：**API 强制要求 email 参数；未配置邮箱时完全不调用 Unpaywall**（代码：`fetcher.py` 中 `if not doi or not email: return None`），配置时复用 `OPENALEX_EMAIL`/`CROSSREF_EMAIL`。

---

## 邮箱参数统一策略（公开非个人访问）

| 情形 | 行为 |
|------|------|
| 平台允许匿名访问（OpenAlex / Crossref）且未配邮箱 | **匿名访问**（标准速率池），不带任何身份参数 |
| 平台允许匿名访问且已配真实邮箱 | 带 `mailto` 进 polite pool（平台官方鼓励的正当机制） |
| 平台强制要求邮箱（Unpaywall）且未配邮箱 | **完全不访问该服务**（优雅跳过，不影响主流程） |
| 任何情况 | **绝不发送占位/伪造邮箱**（本轮已从 OpenAlex/Crossref/openalex_refs 移除 `paper-agent@localhost` 兜底） |

## 全文获取统一策略（OA-only）

```
请求全文 → 源 API 声明的 OA 链接（arXiv / OpenAlex best-OA / Europe PMC 仅OA / DOAJ / HAL fileMain_s / OpenAIRE 仅OA实例 / CORE downloadUrl）
        → Unpaywall best_oa_location（DOI 查询，仅 is_oa=true）
        → 全部落空：回退摘要级回答，明示"该文暂无开放获取全文"
```

- 付费墙出版商链接在代码层面无路径（Europe PMC/OpenAIRE 解析层只放 OA 链接，单测锁定）。
- 下载安全：scheme 白名单 + DNS 逐 IP 公网校验 + 拒云元数据 + 50MB 流式上限 + PDF 魔数校验（SSRF 防护）。
- PDF 仅服务端解析用于问答，**不设对外 PDF 下载端点**（arXiv ToU 明确要求）；用户通过引用链接回到原平台页面。

## 多用户部署的访问频率合规（服务器上线）

平台看到的是我们后端这一个客户端，合规义务由后端统一承担，现有机制：

1. **每源全局限流器**（`tools/search/base.py::RateLimiter`）是**进程级单例**——无论多少用户同时使用，对 arXiv 的出口流量永远是 3 秒 1 请求单连接，对其他源同样被各自 limiter 收敛。用户并发只影响排队，不放大出站频率。
2. **PDF 下载全局限流**：2 并发 / 1s 间隔（进程级），全文下载不会对上游形成突发。
3. **429 处理**：读 `Retry-After` 精确等待或指数退避（≤3 次）。
4. **全局超时**：一次搜索的所有源任务 120s deadline，超时丢弃，不堆积后台请求。
5. **熔断器**（`core/circuit_breaker.py`）：某工具持续失败时自动停止调用，避免对故障上游持续加压。
6. **单 worker 约束**：uvicorn 必须 `--workers 1`（`start.sh prod` 已内置）——限流/熔断均为进程内状态，多 worker 会破坏上述保证。
7. **建议**：上线前申请免费的 `S2_API_KEY`（无 key 时 Semantic Scholar 走全平台共享的匿名配额，多用户场景会频繁 429；不影响合规，只影响体验）。

## 署名（Attribution）

DOAJ（CC BY-SA）、OpenAIRE（CC-BY）等有署名义务；产品内已统一履行：前端 设置 → 数据来源 中列出全部平台及其许可（`frontend/components/SettingsPopover.tsx` + `frontend/lib/i18n.ts` 的 `settings_data_sources_body`）。

## 明确不使用的渠道（红线）

- Sci-Hub 及任何侵权源；
- Google Scholar / ResearchGate / Academia.edu 抓站（无官方开放 API，ToS 禁止抓取）；
- 出版商付费墙页面（Elsevier/Springer/IEEE/Wiley 等）的任何形式的绕过、模拟登录、cookie/EZproxy 代持；
- 未经平台许可的批量镜像/再分发。

---

*本文件为工程合规说明，非法律意见。各平台条款可能更新，上线前请以官方页面为准复核一遍。*
