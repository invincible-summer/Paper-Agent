# 论文平台官方接口与证据边界说明

> 本文件记录当前运行版本实际使用的论文平台能力。平台能力只包括论文搜索、元数据和当前有效摘要获取；网络论文 OA 探测、Unpaywall/doi.org 全文解析、PDF 下载和自动全文升级已经下线。全文级分析必须以用户上传文件为入口。

## 1. 统一运行规则

1. 搜索适配器只读取各平台官方元数据/搜索 API，并保留标题、作者、年份、DOI、来源页、引用数和摘要等字段。
2. 网络论文只有在当前平台摘要能力开启且摘要非空时，才可进入摘要级问答、研究地图和文献综述；摘要为空、摘要能力关闭或仅存在历史缓存时直接排除。
3. 网络论文来源 URL 是落地页/元数据来源，不是全文许可证明；系统不会根据 URL 猜测 PDF，也不会访问出版商付费墙。
4. 用户上传的 PDF、DOCX、TXT、MD、TEX 和图片由本地 sidecar、结构解析、分块 RAG 与可选 VLM 处理。上传原件和派生私有数据保留在当前会话/账号边界内。
5. 不使用未经官方文档授权的抓站、代理、Cookie、校园网、Sci-Hub、ResearchGate 或批量镜像路径。

## 2. 当前接入平台

| 平台 | 当前调用 | 当前证据 |
|---|---|---|
| OpenAlex | 官方 Works 搜索/元数据 API | 元数据、摘要（若记录提供且能力开启） |
| Semantic Scholar | 官方 Graph API（需 Key 与许可确认） | 元数据、摘要（若记录提供且能力开启） |
| arXiv | 官方 Atom API | 元数据、摘要 |
| Crossref | 官方 REST API | DOI 元数据、摘要（若记录提供） |
| Europe PMC | 官方 REST API | 医学/生命科学元数据、摘要 |
| DOAJ | 官方 API | 文章元数据、摘要 |
| HAL | 官方 Solr API | 仓储元数据、摘要 |
| OpenAIRE | 官方 Graph API V3 | 研究产品元数据、摘要（若记录提供） |
| CORE | 官方 API（需 Key 与许可确认） | 聚合元数据、摘要 |
| bioRxiv / medRxiv | 官方元数据 API + 本地元数据索引 | 版本元数据、摘要 |
| PubMed | NLM E-utilities（可选邮箱/Key） | PMID 元数据、摘要（按条款使用） |
| DataCite | 官方 REST API | DOI 元数据、Abstract 描述（若记录提供） |
| DBLP | 官方 Publication Search API | 计算机科学元数据；平台通常不提供摘要 |

平台是否启用由 `data/users.db` 的论文搜索策略管理；公开能力矩阵只显示 `search` 与 `abstract`。诊断只测试连通、搜索和有效摘要，不测试 PDF 魔数、下载速度或 OA 状态。

## 3. 许可与隐私

- 每个平台的 API Key、联系邮箱和许可确认只存在 `.env` 或管理员运行时配置，不写入论文记录、历史、Checkpoint、日志或导出。
- 元数据/摘要的再利用范围以对应平台官方条款为准；本项目只在当前会话中用于检索、引用和证据边界内的分析，不建立网络全文镜像。
- 上传论文的原始文件、文本 sidecar、元素裁图、向量和导出成果属于用户数据，受会话/账号所有权校验保护；不会被网络论文清理迁移删除。

## 4. 退役缓存迁移

升级后后端会执行一次版本标记的 `core.remote_fulltext_retirement`：删除旧网络 PDF、`public_pdf` artifacts、网络论文元素裁图、全文 Chroma chunks、全文 summary/status 和网络 PDF 路径，并清理历史/API Checkpoint 中相应衍生字段。迁移保留用户上传原件/sidecar、`upload:*` 元素和向量、消息历史及既有导出。

预览与显式执行：

```bash
./.env_conda/bin/python scripts/retire_remote_fulltext_cache.py
./.env_conda/bin/python scripts/retire_remote_fulltext_cache.py --execute
```

迁移是幂等的；项目根目录、开发后端目录和独立 `/v1` 存储根均纳入边界。任何云服务器版本专属发布步骤仍以 `Website_deployment_plan.md` 的确认门槛为准。
