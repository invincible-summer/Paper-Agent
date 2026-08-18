# 论文平台官方接口、许可与调用规范

> **最近核对：2026-08-18**
> 本文件是 Paper Agent 论文平台接入的工程合规基线，不构成法律意见。只允许使用平台明确提供的 API、OAI-PMH 或官方数据下载方式；禁止逆向网页内部接口、绕过登录/付费墙、模拟浏览器批量抓取或未经许可再分发全文。

## 1. 判定原则

1. **元数据许可不等于全文许可**：标题、DOI、作者可能可开放复用，但摘要和 PDF 仍可能受出版商或作者许可约束。
2. **公开可读不等于允许镜像/再分发**：Agent 可以在官方条款允许的范围内临时下载 OA 文件做用户请求内分析，但不得因此建立公开 PDF 镜像。
3. **API Key 不等于产品用途许可**：CORE、Semantic Scholar 等平台除技术凭证外还有用途、展示、归因或商业条件。
4. 所有渠道都必须经过管理员开关、运行配置、许可门禁、进程级限流、硬时限和熔断器；失败返回部分结果。
5. 全文只走明确 OA 路径；Crossref、DataCite、DBLP、PubMed 的普通落地链接绝不直接作为 PDF。

## 2. 当前接入总表

| 平台 | 工程状态 | 官方方式 | 认证/许可门禁 | Agent 用途 |
|---|---|---|---|---|
| OpenAlex | 条件启用 | REST API | `OPENALEX_API_KEY` | 综合检索、引用图谱 |
| Semantic Scholar | 条件启用 | Academic Graph API | Key + `S2_LICENSE_CONFIRMED=true` | 学术检索、OA 候选 |
| arXiv | 启用 | Atom API | 无 Key；单连接、3 秒一次 | STEM 预印本检索 |
| Crossref | 启用 | REST API | 真实联系邮箱推荐 | DOI 元数据检索 |
| Europe PMC | 启用 | REST API | 无 Key | 医学/生命科学检索、明确 OA 链接 |
| DOAJ | 启用 | REST API v4 | 无 Key | OA 期刊文章检索 |
| HAL | 启用 | Solr REST API | 无 Key | 开放仓储和学位论文 |
| OpenAIRE | 启用/低频兜底 | Graph API V3 | 匿名低额度或官方 OAuth 客户端 | 欧盟开放研究图谱 |
| CORE | 条件启用 | REST API v3 | Key + `CORE_LICENSE_CONFIRMED=true` | 机构仓储聚合 |
| bioRxiv | 新增、本地索引 | 官方 metadata API | 无 Key；逐记录许可 | 生命科学预印本元数据 |
| medRxiv | 新增、本地索引 | 官方 metadata API | 无 Key；逐记录许可 | 医学预印本元数据 |
| PubMed | 新增、条件启用 | NCBI E-utilities | 真实联系邮箱；Key 可选 | 权威生物医学元数据/摘要 |
| DataCite | 新增、条件启用 | REST API | 无 Key | 数据集、软件、报告、论文和 DOI |
| DBLP | 新增、条件启用 | Publication Search API | 可识别 User-Agent | 计算机科学元数据 |
| Unpaywall | OA 辅助 | REST API | 必须真实邮箱 | DOI → 合法 OA 位置 |
| doi.org | 解析辅助 | HTTPS DOI resolver | 无 | DOI 跳转解析，不判定 OA |
| ChinaXiv | **不接入** | 尚未取得公开 API/OAI 及自动调用许可依据 | — | 禁止网页逆向/抓取 |

新增的 bioRxiv、medRxiv、PubMed、DataCite、DBLP 在已有生产数据库升级后默认关闭，管理员完成配置、许可核对或索引同步后再启用。

## 3. 宽松开放或明确公共接口

### 3.1 OpenAlex

- 官方文档：https://docs.openalex.org/ 、https://help.openalex.org/api/
- 请求：`GET https://api.openalex.org/works?search=...&api_key=...`
- 元数据：OpenAlex 数据集按 CC0 发布。
- 当前规则：自 2026-02-13 起 API 请求需要 API Key；项目不再把旧 `mailto` polite-pool 机制当作生产配额。
- 实现：没有 `OPENALEX_API_KEY` 时不进入智能路由；只接受 `best_oa_location.pdf_url` 或 `primary_location.pdf_url`，不把 OA 落地页冒充 PDF。

### 3.2 arXiv

- API 手册：https://info.arxiv.org/help/api/user-manual.html
- API 条款：https://info.arxiv.org/help/api/tou.html
- 请求：`GET https://export.arxiv.org/api/query?search_query=...`
- 速率：单连接，连续请求至少间隔 3 秒。
- 元数据可按其声明复用；论文全文许可由作者逐篇选择。Agent 只做临时分析缓存，不提供 arXiv PDF 公开下载端点。
- 实现：每轮来源级批处理，最多两个英文请求；本地限流排队取消不得记作 arXiv 故障。

### 3.3 Crossref

- 文档：https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- Etiquette：https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-retrieval/
- 请求：`GET https://api.crossref.org/works?query=...&rows=...&mailto=...`
- Crossref 汇聚成员提交的数据，字段许可可能不同；项目只使用检索所需元数据。
- `link` 表示登记链接，不证明开放获取，因此本项目不再直接把 Crossref PDF link 交给下载器，而是通过 Unpaywall/OA 校验。

### 3.4 Europe PMC

- 文档：https://europepmc.org/RestfulWebService
- 请求：`GET https://www.ebi.ac.uk/europepmc/webservices/rest/search`
- 用途：医学、生命科学、PubMed 内容和预印本检索。
- 只接受响应明确标为 OA 的全文 URL；文章正文和摘要仍按单篇许可及来源条款处理。

### 3.5 DOAJ

- API：https://doaj.org/api/v4/docs
- 公共数据许可：https://doaj.org/docs/public-data-dump/
- 请求：`GET https://doaj.org/api/search/articles/{query}`
- DOAJ 文章元数据按 **CC0** 提供；期刊和文章正文采用各自开放许可。
- 只有 DOAJ 记录同时标为 fulltext 且明确声明 PDF MIME/type 的链接才可作为候选；HTML 落地页不生成 PDF 候选，后续仍验证 PDF 魔数。

### 3.6 HAL

- API：https://api.archives-ouvertes.fr/docs/search/
- 开放数据说明：https://doc.hal.science/en/api/
- 请求：`GET https://api.archives-ouvertes.fr/search/?q=...&fl=...&wt=json`
- 项目按官方 Solr 语法转义查询；`fileMain_s` 是 HAL 仓储文件候选，使用时仍尊重记录许可。

### 3.7 OpenAIRE

- Graph API V3：https://graph.openaire.eu/docs/apis/graph-api/
- Research Products：https://graph.openaire.eu/docs/apis/graph-api/research-products/
- 认证：https://graph.openaire.eu/docs/apis/authorization-and-authentication/
- 请求：`GET https://api.openaire.eu/graph/v3/research-products?search=...&type=publication`
- Graph 数据按官方 CC-BY 政策使用并保留来源归因。
- 匿名调用约 60 次/小时，认证服务约 7200 次/小时；项目把匿名本地预算进一步收紧为 50 次/小时，无服务凭证时仅作低频兜底，不再使用旧 `/search/publications` 接口。

### 3.8 DataCite

- REST 文档：https://support.datacite.org/docs/api
- DOI 检索：https://support.datacite.org/docs/api-get-dois
- 请求：`GET https://api.datacite.org/dois?query=...&resource-type-id=text&page[size]=...`
- DataCite 元数据按 CC0 提供。
- 项目只用于数据集、软件、报告、学位论文、论文元数据和 DOI 意图/兜底；记录 URL 是落地页，不是 PDF/OA 证明。

### 3.9 DBLP

- Search API：https://dblp.org/faq/How+to+use+the+dblp+search+API.html
- 数据许可：https://dblp.org/faq/What+is+the+license+of+the+dblp+dataset.html
- 请求：`GET https://dblp.org/search/publ/api?q=...&format=json&h=...`
- 主站发生 5xx 时只回退到 DBLP/Schloss Dagstuhl 官方镜像 `https://dblp.dagstuhl.de/search/publ/api`，不使用第三方抓取镜像。
- DBLP 元数据 CC0；项目发送可识别 User-Agent，仅用于计算机科学路由，不抓取网页或猜测 PDF。

## 4. 条件允许的平台

### 4.1 Semantic Scholar

- API：https://www.semanticscholar.org/product/api
- License：https://api.semanticscholar.org/license/
- 请求：`GET https://api.semanticscholar.org/graph/v1/paper/search`，Header `x-api-key`。
- API License 包含展示、归因、用途和商业使用条件，不能仅凭“免费 Key”断言任意用途均可。
- 项目要求 `S2_API_KEY` 与 `S2_LICENSE_CONFIRMED=true` 同时存在；管理员页面开关不能绕过该环境门禁。

### 4.2 CORE

- 官方服务页：https://core.ac.uk/services/api
- API 文档：https://api.core.ac.uk/docs/v3
- 请求：`GET https://api.core.ac.uk/v3/search/works/?q=...&limit=...`，Header `Authorization: Bearer ...`。
- **必须保留尾斜杠**：无尾斜杠会返回 301；本项目已修正，且非预期 3xx 不再伪装成“可达但零结果”。
- CORE 条款对搜索/发现产品、机构和商业用途有额外许可要求。项目要求 Key 与 `CORE_LICENSE_CONFIRMED=true` 同时存在。

### 4.3 PubMed / NCBI

- E-utilities 总览：https://www.ncbi.nlm.nih.gov/books/NBK25501/
- 使用规则：https://www.ncbi.nlm.nih.gov/books/NBK25497/
- PubMed 数据条款：https://www.nlm.nih.gov/databases/download/terms_and_conditions.html
- 调用：ESearch 获取 PMID，再用 EFetch 批量获取 XML；发送工具名和真实联系邮箱，可选 `NCBI_API_KEY`。
- 速率：无 Key 不超过 3 请求/秒；有 Key 通常不超过 10 请求/秒。
- PubMed 记录由多来源组成，部分摘要和字段受版权保护；项目只做用户查询所需展示和分析，并保留 NLM/来源免责声明，不下载或再分发出版商 PDF。

### 4.4 bioRxiv / medRxiv

- 官方 API：https://api.biorxiv.org/
- API 支持按 server、日期区间、游标或 DOI 获取元数据，**不提供任意历史关键词搜索**。
- 项目不调用网页搜索，而是通过官方 API 增量同步元数据到 `data/paper_source_catalog.db` 的 FTS5 索引。
- 本地只保存元数据、版本、来源页和许可标识，不保存 PDF。正文许可逐篇判断；没有明确 OA/许可证明时不生成 PDF 候选。

### 4.5 Unpaywall

- API/条款：https://unpaywall.org/products/api 、https://unpaywall.org/legal/terms-of-service
- 请求：`GET https://api.unpaywall.org/v2/{doi}?email=...`
- 必须配置真实 `PAPER_PLATFORM_CONTACT_EMAIL`（旧联系邮箱仅兼容回退）。
- 只接受 `is_oa=true`，优先 `best_oa_location.url_for_pdf`；不镜像 Unpaywall 数据库。

## 5. 暂不接入与禁止路径

### ChinaXiv

截至 2026-08-18，本轮未取得 ChinaXiv 官网公开、稳定、面向第三方开发的关键词 API/OAI-PMH 文档及自动调用许可依据。因此：

- 不实现 ChinaXiv 搜索后端；
- 不逆向网页 XHR、内部 JSON、验证码或 Cookie 接口；
- 不批量抓取搜索结果页；
- 不按 URL 规律猜测 PDF。

取得官方书面文档或许可后，必须重新完成接口、速率、元数据许可和全文许可审计才能接入。

同样禁止：Sci-Hub；Google Scholar/ResearchGate/Academia.edu 抓站；出版商付费墙绕过；校园代理、Cookie、EZproxy 代持；未经授权的批量全文镜像。

## 6. 运行时合规控制

- 默认智能路由只选最多 4 个主渠道，结果不足且时间允许时最多增加 2 个兜底渠道。
- 每个平台每轮只有一个来源任务；远程来源最多消费两条检索式，避免“平台 × 检索式”请求爆炸。
- arXiv 保持单连接/3 秒；NCBI 3/10 RPS；OpenAIRE 匿名小时预算；其他源使用保守进程级 limiter。
- `SearchOutcome` 区分远端错误与 `local_budget_exhausted`；本地限流队列取消不会触发上游熔断。
- 非预期重定向、HTML 机器人挑战、响应 schema 改变、429 和 5xx 分别记录，达到 30 秒硬时限即返回部分结果。
- OA 下载继续执行公网 DNS/IP 校验、逐跳重定向校验、大小上限、PDF 魔数验证和全局下载限流。

## 7. 部署运营者检查

上线前至少确认：

1. `PAPER_PLATFORM_CONTACT_EMAIL` 是真实可联系地址；
2. OpenAlex 已配置 `OPENALEX_API_KEY`；
3. 只有确认符合对应 License 时才设置 `S2_LICENSE_CONFIRMED=true` 或 `CORE_LICENSE_CONFIRMED=true`；
4. OpenAIRE 服务凭证和 NCBI Key 只从各自官方渠道申请；
5. bioRxiv/medRxiv 本地索引只由官方 API timer 写入；
6. 管理员页面显示的平台协议、许可状态和运行状态符合预期；
7. 条款变化时更新本文件及运行门禁，而不是仅修改文字说明。
