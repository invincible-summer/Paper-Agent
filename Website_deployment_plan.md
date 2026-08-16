# Paper Agent 生产部署手册（3–6 人并发 · 阿里云 ECS）

> **适用目标**：一台服务器同时承载清小搭 OpenAI 兼容接口、FastAPI 后端，以及可选的 Next.js 自有前端；按 **3–6 个混合并发用户**设计。软件仍保留账号隔离、会话隔离、限流、熔断和后续横向扩展边界。
>
> **强制协议基线**：清小搭接入必须严格遵循仓库根目录 `openai-compatible-agent-integration-guide.md`。生产环境只通过 HTTPS 暴露服务。

## 0. 结论先行

### 0.1 正式规格

| 项目 | 正式建议 | 说明 |
|---|---:|---|
| ECS | **4 vCPU / 16 GiB** | 可承受 3–6 个混合用户；Docling、嵌入和精排首次并发会产生明显 CPU/内存峰值 |
| 系统 | Ubuntu Server 22.04 LTS x86_64 | 本手册按此编写 |
| 系统盘 | **100 GiB ESSD PL1** | 模型缓存、PDF、元素裁图、Chroma、SQLite、导出和日志会持续增长 |
| 公网带宽 | **10 Mbps 固定带宽** | PDF/DOCX 上传下载更稳定；若几乎不传文件，5 Mbps 可作为最低档 |
| Swap | **4 GiB** | 只用于吸收瞬时峰值，不能替代内存 |
| Uvicorn | **1 worker，不能增加** | 会话内存、熔断器、限流信号量都是进程内状态 |

**不建议把 2C8G 或 4C8G 作为正式 3–6 人生产规格。** 它们可用于个人调试或纯清小搭轻对话，但在 Docling OCR、深读、嵌入/精排和导出重叠时容易排队或触发内存压力。若经常出现 6 路重任务（多份扫描 PDF 同时 OCR/深读），直接升级到 **8C32G**，不要增加 Uvicorn worker。

### 0.1.1 仅向清小搭提供 API 时，2 vCPU / 4 GiB 是否可用

**可以作为低成本起步规格，但不是 3–6 路重型深读并发规格。** 适用边界必须同时满足：不运行 Next.js、自有前端用户为 0；3–6 人以文字问答/检索为主；同一时刻最多 1 路 PDF 深读、OCR、VLM 或 DOCX 导出；配置 4 GiB swap；启用本文的 API 独立 7 天 Checkpoint、磁盘阈值和 hourly cleanup timer。首次 Docling/embedding/reranker 加载仍可能接近内存上限，建议在 2C4G 上关闭本地 reranker，扫描 PDF 排队处理。

| 规格 | 推荐场景 | 明确限制 |
|---|---|---|
| **2 vCPU / 4 GiB / 60–100 GiB** | 仅 `/v1`，3–6 个账号轻量交替使用 | 重任务并发 1；可能使用 swap；首次深读慢；不承诺 6 路混合负载 |
| **4 vCPU / 16 GiB / 100 GiB** | 3–6 人混合使用，含前端或并发深读 | 本手册正式验收规格 |

2C4G 出现 OOM、swap 持续增长、单次深读长期阻塞文字 SSE 或磁盘频繁达到 85% 时，直接升配到 4C16G；**不要通过增加 Uvicorn worker 规避资源不足**。

### 0.2 验收负载

上线前至少同时制造以下 6 路请求并观察 15 分钟：

1. 3 路普通流式对话；
2. 1 路文献检索；
3. 1 路 PDF 深读/OCR；
4. 1 路 DOCX 导出与下载。

验收目标：没有 OOM、服务不重启、SSE 持续有帧、错误率为 0；CPU 短时 100% 可以接受，但不应长时间无响应。内存持续超过 13 GiB、Swap 持续增长或深读长期排队时升级 8C32G。

---

## 1. 架构与端口

```text
清小搭网关 ── HTTPS ──┐
浏览器（可选前端）───┼── nginx :443
                       ├── /v1/*, /api/v1/*, /files/*, /elements/* → FastAPI :8000
                       └── /                                         → Next.js :3000

FastAPI（单 worker）
  ├── DeepSeek 文本 LLM
  ├── 可选 OpenAI-compatible VLM
  ├── OpenAlex/arXiv/S2 等公开论文源
  └── SQLite + Chroma + 本地文件（单机持久化）
```

- `/v1/models`、`/v1/chat/completions`：清小搭渠道，仅接受 Bearer Agent API Key。
- `/api/v1/*`：自有前端渠道，使用浏览器登录令牌；生产建议禁止游客。
- `/files/*`：清小搭生成的 API 公共别名可公开短期读取；自有前端生成的 Web 导出必须携带浏览器登录/游客身份，按所有者校验。不要用 Basic Auth 覆盖整站。
- `/elements/assets/*`：论文元素图片；如自有前端需要展示，必须转发。
- 8000、3000 只监听 `127.0.0.1`，安全组不得放行。

---

## 2. 创建 ECS 与安全组

选择：

- 4 vCPU / 16 GiB；
- Ubuntu 22.04 64 位；
- 100 GiB ESSD PL1；
- 公网 IPv4；
- 10 Mbps 固定带宽；
- 地域尽量靠近主要用户和所用 LLM 服务。

安全组只开放：

| 端口 | 来源 | 用途 |
|---|---|---|
| 22 | 管理员固定公网 IP `/32` | SSH；不要对全网开放 |
| 80 | `0.0.0.0/0` | ACME/HTTP 跳转 HTTPS |
| 443 | `0.0.0.0/0` | 清小搭与浏览器 HTTPS |

优先使用 SSH 密钥而不是 root 密码。首次登录可用 root，应用服务必须改用专用低权限用户。

---

## 3. 安装系统运行环境

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential nginx \
  software-properties-common poppler-utils tesseract-ocr \
  libgl1 libglib2.0-0

# Ubuntu 22.04 默认只有 Python 3.10；项目要求 Python >= 3.11。
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
python3.11 --version              # 必须是 Python 3.11.x
```

> `ppa:deadsnakes/ppa` 是 Ubuntu 22.04 上安装并行 Python 3.11 的明确步骤；不要替换或删除系统自带 Python 3.10。若组织策略禁止 PPA，改用经过校验的 Miniforge Python 3.11，并把下文所有 `python3.11` 保持指向该解释器。

安装 **Node.js 22 LTS** 后启用 Corepack/pnpm。本手册固定 Node 22 LTS，仓库已在该版本验证；Next.js 14 的最低要求为 Node 18.17：

```bash
node --version                    # 应为 v22.x
sudo corepack enable --install-directory /usr/local/bin
corepack prepare pnpm@11.9.0 --activate
command -v pnpm                   # 记录绝对路径，systemd 要使用它
pnpm --version                    # 应为 11.9.0
```

创建服务账号和目录：

```bash
sudo useradd --system --create-home --home-dir /var/lib/paper-agent \
  --shell /usr/sbin/nologin paper-agent
sudo mkdir -p /opt/paper-agent /var/lib/paper-agent/cache
sudo chown -R paper-agent:paper-agent /opt/paper-agent /var/lib/paper-agent
```

创建 4 GiB Swap：

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 4. 上传代码与安装依赖

推荐以 `paper-agent` 用户拉取代码：

```bash
sudo -u paper-agent git clone <你的私有仓库地址> /opt/paper-agent
cd /opt/paper-agent
sudo -u paper-agent python3.11 -m venv .venv
sudo -u paper-agent .venv/bin/pip install --upgrade pip
sudo -u paper-agent .venv/bin/pip install -r requirements.txt -e backend
sudo -u paper-agent bash -lc 'cd /opt/paper-agent/frontend && pnpm install --frozen-lockfile'
```

如果使用压缩包上传，必须排除 `.env`、`*.key`、`*.pem`、`.git`、`.next`、`node_modules`、运行时 `data/`、`history_record/` 和本机缓存。

### 4.0 部署必须从空运行目录开始（不要上传本地旧数据）

**源码部署与运行数据部署是两件事**。云服务器首次上线只同步 Git 源码和必要的静态配置模板，不能把开发机的历史数据目录复制过去。以下目录/文件均属于本地运行数据，默认不上传、不从开发机恢复、不放入压缩包：

- `history_record/`、`backend/history_record/`：对话、思考/工具轨迹和会话论文集合；
- `data/`、`backend/data/`：用户数据库、论文 PDF、上传原文件及 `.txt` sidecar、元素裁剪、Chroma/SQLite、视觉缓存、导出、日志、临时文件；
- `.env`、API keys、管理员密钥、模型缓存、`.next/`、`node_modules/`、`.env_conda/`。

推荐用 `git clone` 获取干净工作树；如果必须从本地发送压缩包，先在**发送端**执行：

```bash
# 在项目根目录执行；不要把输出包放进项目根目录后再打包。
tar --exclude=.git --exclude=.env --exclude='.env.*' \
  --exclude='data' --exclude='backend/data' \
  --exclude='history_record' --exclude='backend/history_record' \
  --exclude='frontend/.next' --exclude='frontend/node_modules' \
  --exclude='.env_conda' --exclude='__pycache__' \
  -czf /tmp/paper-agent-source.tar.gz .
```

服务器上不要先复制开发机的 `data/` 或 `history_record/`，而是创建全新的空目录并限制权限：

```bash
sudo -u paper-agent mkdir -p /opt/paper-agent/data /opt/paper-agent/history_record
sudo chmod 700 /opt/paper-agent/data /opt/paper-agent/history_record
# 首次启动会按当前生产配置创建新的 users.db、metadata.db、Chroma 和缓存。
```

只有在明确执行“数据迁移/灾备恢复”并完成加密传输、权限审查和停机窗口后，才允许恢复指定的生产备份；**开发机旧对话、旧论文和旧上传不属于默认部署输入**。

### 4.1 预下载并真正加载模型

不能只下载文件后立刻设置 `HF_HUB_OFFLINE=1`；必须用服务账号实际构造嵌入器、精排器和 Docling 转换器：

```bash
cd /opt/paper-agent
sudo -u paper-agent env \
  XDG_CACHE_HOME=/var/lib/paper-agent/cache \
  HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python - <<'PY'
from core.embeddings import get_embedder
from tools.retrieval.rerank import get_reranker
from tools.pdf.structure import get_structure_parser

assert get_embedder() is not None, "embedding model load failed"
assert get_reranker() is not None, "reranker model load failed"
parser = get_structure_parser()
if hasattr(parser, "_ensure_converter"):
    parser._ensure_converter()  # 初始化 Docling layout/OCR/table pipeline
print("embedding, reranker and Docling are ready")
PY
```

首次加载 Docling 可能下载数百 MiB 模型；默认精排模型约 1.1 GiB。完成后才在生产 `.env` 中启用离线模式。部署后仍需用真实 PDF 做一次解析验收。

---

## 5. 生产环境变量

```bash
cd /opt/paper-agent
sudo -u paper-agent cp .env.example .env
sudo chmod 600 .env
sudo chown paper-agent:paper-agent .env
sudo -u paper-agent nano .env
```

生产最小配置（示例中没有真实密钥）：

```ini
APP_ENV=production
APP_DEBUG=false
APP_HOST=127.0.0.1
APP_PORT=8000
PUBLIC_BASE_URL=https://paper-agent.example.com

DEEPSEEK_API_KEY=<填写文本模型服务商密钥>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_LIGHT=<填写实际可用模型名>
DEEPSEEK_MODEL_REASONING=<填写实际可用模型名>

# 自有前端生产策略：要求账号登录、关闭公开注册、禁止游客。
AUTH_REQUIRED=true
REGISTRATION_OPEN=false
GUEST_ACCESS=false
FRONTEND_ORIGIN=https://paper-agent.example.com
CORS_ORIGINS=

# 模型已按 §4.1 预热后再开启。
HF_HUB_OFFLINE=1
XDG_CACHE_HOME=/var/lib/paper-agent/cache

# 可选；留空时视觉能力优雅降级，不影响文本主流程。
MULTIMODAL_BASE_URL=
MULTIMODAL_API_KEY=
MULTIMODAL_MODEL=

# 仅 /v1 的独立临时存储；自制前端持久化不受这些 TTL/清理策略影响。
OPENAI_API_STORAGE_ROOT=/opt/paper-agent/data/openai_api
OPENAI_API_POLICY_PRESET=balanced
```

说明：

- `PUBLIC_BASE_URL` 必须是外部 HTTPS Origin，不带 `/v1`。清小搭附件中的 `fileUrl` 由它确定，避免反向代理下生成错误的内网 URL。
- `DEEPSEEK_*`/`MULTIMODAL_*` 是模型服务商密钥；**Agent API Key 不是模型密钥**。
- `AGENT_API_KEY` 仍可作为迁移/应急凭证，但正式部署推荐由管理员生成数据库密钥；生产环境没有任何可用 Agent Key 时 `/v1` 返回 503，错误或已撤销密钥返回 401。
- `.env`、`*.key`、`*.pem` 不得提交、截图、复制到工单或日志中；仓库只保留无真实值的 `.env.example`。

---

## 6. 初始化管理员与 Agent API Key

管理员固定标识：

- 用户名：`administrator`
- 邮箱：`administrator@administrator`

密码只通过终端隐藏输入，不允许作为命令行参数、环境变量或文档文本：

```bash
cd /opt/paper-agent
sudo -u paper-agent .venv/bin/python scripts/bootstrap_administrator.py
```

脚本幂等：管理员已存在时不会改密码，也不会把同名普通用户自动提权。

### 6.1 生成给清小搭的长期密钥

**方法 A（无需部署前端，推荐用于首次接入）**：

```bash
sudo -u paper-agent .venv/bin/python scripts/create_agent_api_key.py \
  --name '清小搭生产接入'
```

输入管理员密码后，脚本只显示一次完整 `pa_live_...` 密钥。立即存入密码管理器；数据库只保存 SHA-256，不可恢复明文。

**方法 B（部署前端后）**：管理员登录 `/login`，进入“管理 → Agent 接入管理”，创建并复制密钥。页面同样只显示一次完整值，可随时查看使用时间或撤销。

密钥长期有效，直到管理员主动撤销。不要把管理员登录令牌、Agent API Key 与 DeepSeek/VLM Key 混用。

---

## 7. systemd：后端单 worker

创建 `/etc/systemd/system/paper-agent.service`：

```ini
[Unit]
Description=Paper Agent FastAPI backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=paper-agent
Group=paper-agent
WorkingDirectory=/opt/paper-agent
EnvironmentFile=/opt/paper-agent/.env
Environment=PYTHONUNBUFFERED=1
Environment=XDG_CACHE_HOME=/var/lib/paper-agent/cache
ExecStart=/opt/paper-agent/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=3
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/paper-agent/data /opt/paper-agent/history_record /var/lib/paper-agent

[Install]
WantedBy=multi-user.target
```

先创建所有写目录再启动，否则 `ProtectSystem=strict` 会阻止运行时创建它们：

```bash
sudo -u paper-agent mkdir -p /opt/paper-agent/data \
  /opt/paper-agent/history_record
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent
sudo systemctl status paper-agent --no-pager
curl -s http://127.0.0.1:8000/health
```

`--workers 1` 是当前单机架构约束。要扩容到多 worker/多机，必须先把会话状态、熔断器、限流和任务队列迁移到共享外部服务；本手册不允许直接增加 worker 数。

---

## 8. 可选 Next.js 前端服务

清小搭接入不依赖前端；但推荐部署前端供管理员管理密钥和少量用户直接使用。

构建时把 SSE 直连地址设为同一个公网 Origin：

```bash
cd /opt/paper-agent/frontend
sudo -u paper-agent env \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.example.com \
  pnpm build
```

### 7.1 OpenAI API 存储清理 timer（必须启用）

仓库提供 `deploy/systemd/paper-agent-cleanup.service` 和 `.timer`。安装前先确保目录和权限：

```bash
sudo -u paper-agent mkdir -p /opt/paper-agent/data/openai_api
sudo chmod 700 /opt/paper-agent/data/openai_api
sudo cp /opt/paper-agent/deploy/systemd/paper-agent-cleanup.service /etc/systemd/system/
sudo cp /opt/paper-agent/deploy/systemd/paper-agent-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent-cleanup.timer
sudo systemctl start paper-agent-cleanup.service
sudo systemctl status paper-agent-cleanup.timer --no-pager
sudo journalctl -u paper-agent-cleanup.service -n 50 --no-pager
```

unit 使用 `UMask=0077`、低 CPU/IO 优先级、`ProtectSystem=strict`，唯一可写路径是 `/opt/paper-agent/data/openai_api`。timer 的关键值为 `OnBootSec=10min`、`OnUnitActiveSec=1h`、`RandomizedDelaySec=5min`、`Persistent=true`。它不能扫描或删除 `history_record`、web uploads、web Chroma、`users.db` 或自制前端导出。

管理员页面 `/admin/api-storage` 可查看容量、磁盘状态、清理记录和策略；危险修改、立即清理、紧急删除与遗留扫描必须预览并二次确认。命令行仅用于维护窗口：

```bash
sudo -u paper-agent /opt/paper-agent/.venv/bin/python   /opt/paper-agent/scripts/cleanup_openai_api_storage.py --preview
# 将上一步一次性 token 原样传回；token 10 分钟过期且只能使用一次。
sudo -u paper-agent /opt/paper-agent/.venv/bin/python   /opt/paper-agent/scripts/cleanup_openai_api_storage.py --execute-token '<token>'
```

### 7.2 可选自有前端 systemd

创建 `/etc/systemd/system/paper-agent-web.service`：

```ini
[Unit]
Description=Paper Agent Next.js frontend
After=network-online.target paper-agent.service
Wants=network-online.target

[Service]
Type=simple
User=paper-agent
Group=paper-agent
WorkingDirectory=/opt/paper-agent/frontend
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=127.0.0.1
Environment=BACKEND_URL=http://127.0.0.1:8000
ExecStart=/usr/local/bin/pnpm start
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/paper-agent/frontend/.next /var/lib/paper-agent

[Install]
WantedBy=multi-user.target
```

若 §3 的 `command -v pnpm` 不是 `/usr/local/bin/pnpm`，必须用实际绝对路径替换 `ExecStart`；不要依赖 systemd 的交互式 shell PATH。启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent-web
```

---

## 9. nginx、域名与 HTTPS

正式环境必须准备域名、DNS、ICP备案（中国大陆服务器）和有效 TLS 证书。不要用明文 HTTP 传递 Bearer 密钥。

单域名推荐配置（证书路径替换为实际值）：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name paper-agent.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name paper-agent.example.com;

    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    client_max_body_size 50m;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        add_header X-Accel-Buffering no always;
    }

    location /api/v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 600s;
        add_header X-Accel-Buffering no always;
    }

    location /files/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /elements/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /health {
        proxy_pass http://127.0.0.1:8000;
    }

    # 可选前端；若不部署前端，可将此 location 改为 return 404。
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

**不要**在 `/v1` 或 `/files` 上配置 nginx Basic Auth：前者会破坏清小搭 Bearer 探测，后者会让清小搭无法转存附件。`/api/v1` 自身已有账号令牌校验；生产通过 `AUTH_REQUIRED=true + GUEST_ACCESS=false` 关闭匿名使用。

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/paper-agent /etc/nginx/sites-enabled/paper-agent
sudo nginx -t
sudo systemctl reload nginx
```

---

## 10. 严格接入清小搭

在清小搭“标准协议接入”中填写：

| 字段 | 值 |
|---|---|
| API 地址 / baseUrl | `https://paper-agent.example.com/v1` |
| API 密钥 / credential | §6 创建并安全保存的 `pa_live_...` |
| 鉴权方式 | Bearer Token |
| 流式终止符 | `[DONE]` |
| usage 位置 | stop 帧内 |
| 模型 | 可缺失、空或 `paper-agent`；服务端不依赖该字段路由 |

实现保证：

- `GET /v1/models`；
- `POST /v1/chat/completions` 非流式 JSON 与流式 SSE；
- 严格 JSON 布尔 `stream`，接受 `max_tokens: 1` 和缺失/空/null `model`；
- SSE 首帧 `delta.role=assistant`，正文/思考增量随后到达，stop 帧含白名单 `finish_reason` 与 `usage`，最后 `data: [DONE]`；
- 推理内容映射到 `delta.reasoning`，正常回答映射到 `delta.content`；内部 `use_skill` 不形成公开工具事件；
- 文件附件使用 `x_soda.attachments`，包含 `fileUrl/fileName/fileType/mimeType`，并提供 `fileSize`；
- 清小搭不会执行本项目 React 工具卡；research_map 通过自然语言正文 + Markdown 报告 + 静态 SVG 谱系图附件兼容展示，完整交互图谱仍在自有 `/chat` 前端；
- 无效或已撤销凭证返回 401；生产未配置任何 Agent Key 返回 503。

部署后自测（不要把真实 key 写进脚本或 shell 历史；推荐临时从密码管理器读入）：

```bash
read -rsp 'Agent API Key: ' KEY; echo
BASE='https://paper-agent.example.com/v1'

curl -i "$BASE/models" -H "Authorization: Bearer $KEY"
curl -sS -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}]}'
curl -N -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"stream":true,"max_tokens":1,"messages":[{"role":"user","content":"你好"}]}'
unset KEY
```

预期清小搭探测的 connectivity、credential、minimalChat 全部通过，responseFormat 也应通过；随后用“试聊”验证真实逐帧输出。

---

## 11. 多用户策略

软件层始终按多人设计：

- 账号历史按 `user_id` 隔离；
- 浏览器令牌和 Agent API Key 只存哈希；
- 清小搭会话与自有前端会话分开；
- RAG 检索按当前会话论文集合过滤；
- 跨会话零记忆；
- 单 worker 内有 LLM/VLM 并发信号量和外部论文源限流。

生产建议：

```ini
AUTH_REQUIRED=true
REGISTRATION_OPEN=false
GUEST_ACCESS=false
```

如果需要为 3–6 位自有前端用户开户，可在受控时间窗口临时设置 `REGISTRATION_OPEN=true`，创建完账号后立即恢复 `false` 并重启后端。不要开放游客来替代账号管理。

---

## 12. 上线验收清单

### 协议与鉴权

- [ ] 正确 Agent Key 调 `/v1/models` 返回 200；错误/已撤销 key 返回 401。
- [ ] `stream:"false"`、`stream:1`、`stream:null` 返回 422；JSON `false/true` 正常。
- [ ] 流式首帧 role、后续 reasoning/content、stop+usage、`[DONE]` 顺序正确。
- [ ] 清小搭四项探测全绿，真实试聊成功。
- [ ] 管理员页面能创建/一次性显示/撤销密钥；普通用户访问返回 403。
- [ ] 云服务器工作树不含开发机 `history_record/`、`data/`、`backend/data/`、旧 PDF、旧上传或旧数据库；首次启动后这些目录才由生产实例新建。

### 前端与文件

- [ ] `GUEST_ACCESS=false` 时未登录访问 `/chat` 被送到 `/login`。
- [ ] 从真实前端调用真实 LLM，思考折叠区持续流式显示；只隐藏内部工具调用机制，不删除正常思考内容。
- [ ] 上传 PNG/JPG/WebP、PDF 和含图片 DOCX 后可预览/按需理解；VLM 缺失时明确降级。
- [ ] 深读在线论文与用户上传文件走同一多模态结构解析/VLM/RAG 缓存路径。
- [ ] 导出长中文文件名 `.docx` 可下载并是有效 OOXML ZIP；同时抽查 md/txt/tex。
- [ ] 清小搭响应中的附件 URL 是公网 HTTPS 且可直接下载。
- [ ] research_map 在清小搭侧出现 Markdown 文件卡和静态 SVG 图像文件卡；不要把自有 React 卡片是否出现作为协议验收项。

### 容量与安全

- [ ] 完成 §0.2 六路混合并发验收，无 OOM/重启/断流。
- [ ] 只开放 22/80/443；8000/3000 仅监听 127.0.0.1。
- [ ] `.env` 权限 600，Git 仅跟踪 `.env.example`，日志中没有 key/密码/论文原文。
- [ ] systemd 单 worker、自愈和开机启动正常；`paper-agent-cleanup.timer` 已启用且手动 oneshot 成功。
- [ ] `/admin/api-storage` 管理员可访问、普通用户 403；策略解释、危险预览/二次确认、两种 95% 策略和 Trace 模式可用。
- [ ] 模拟 75/85/95/98% 阈值时，API 文件策略符合预期，文字问答继续，web 历史和文件保持字节不变。
- [ ] DeepSeek/VLM 配额、ECS CPU/内存/磁盘设置告警。

---

## 13. 运维、备份与升级

常用命令：

```bash
sudo journalctl -u paper-agent -f
sudo journalctl -u paper-agent-web -f
sudo journalctl -u paper-agent-cleanup.service -n 50 --no-pager
sudo systemctl list-timers paper-agent-cleanup.timer
sudo systemctl restart paper-agent paper-agent-web
sudo systemctl status paper-agent paper-agent-web --no-pager
sudo nginx -t
free -h
df -h
```

建议：

- journald 配置磁盘上限或按月轮转；
- 当前 production unit 的工作目录是 `/opt/paper-agent`，所以运行数据统一写入 `/opt/paper-agent/data`；不要把开发模式下可能出现的 `backend/data` 当成生产主数据；
- 生产第一次部署必须保持 `/opt/paper-agent/data` 和 `/opt/paper-agent/history_record` 为空，由服务首次运行创建新库；不要把开发机旧数据混入部署。
- 日常备份只备份**已经在生产产生**且明确需要保留的自有前端长期数据、`data/users.db`、配置和管理员策略；备份必须加密并限制权限。备份不是新服务器首次部署的默认输入；
- **默认不要长期备份** `data/openai_api/blobs`、`tmp`、`traces`、exports 或过期 Checkpoint，否则备份会绕过 7 天/24 小时业务 TTL；如确需备份 API Checkpoint，备份自身的保留期也不得超过对应 TTL；
- hourly timer 自动处理 API 过期数据；每周检查磁盘和清理记录，不要再写会误删 web 数据的通用 `find data -delete`；
- 对 ECS CPU、内存、Swap、磁盘 >80%、服务重启次数、LLM 429/5xx 设置告警；
- 更新代码后先在维护窗口运行测试和 `pnpm build`，再依次重启后端与前端。

示例备份：

```bash
# 只导出 API 策略，不复制含 Checkpoint/HMAC secret 的整个 state.db。
sudo -u paper-agent /opt/paper-agent/.venv/bin/python - <<'PY'
import json
from dataclasses import asdict
from pathlib import Path
from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext
store = ApiStorageStore(StorageContext.openai_api())
store.initialize()
out = Path("/var/backups/api-storage-policy.json")
out.write_text(json.dumps(asdict(store.get_policy()), ensure_ascii=False, indent=2))
PY
sudo tar czf /var/backups/paper-agent-$(date +%F).tar.gz \
  /opt/paper-agent/data/users.db /opt/paper-agent/history_record \
  /opt/paper-agent/config /var/backups/api-storage-policy.json
sudo chmod 600 /var/backups/paper-agent-*.tar.gz /var/backups/api-storage-policy.json
```

### 何时升级

- **升级 8C32G**：频繁 6 路 OCR/深读、内存长期 >13 GiB、Swap 持续使用或 CPU 长时间满载。
- **不要直接多 worker**：先外置 Redis/任务队列/共享限流与会话状态，再设计多进程。
- 数据持续增长时优先扩系统盘并制定文件生命周期，不要只依赖清理缓存。

---

## 14. 常见问题

**清小搭返回 401**：检查是否粘贴了完整 Agent API Key、是否已撤销，以及 Bearer 前后是否混入额外字符。不要拿 DeepSeek Key 代替。

**清小搭返回 503**：生产环境没有任何 Agent API Key。先运行 §6 的交互式脚本或从管理员页面创建。

**SSE 一次性整段出现**：确认 nginx `/v1/` 和 `/api/v1/` 已 `proxy_buffering off`、`X-Accel-Buffering: no`，并确认中间 CDN 没有缓存流。

**附件 URL 指向内网或 HTTP**：修正 `.env` 的 `PUBLIC_BASE_URL=https://你的域名` 后重启后端。

**PDF 首次深读很慢**：Docling 首次模型加载和 CPU OCR 本来就重；确认 §4.1 已由服务账号预热。不要通过增加 Uvicorn worker 解决。

**管理员密码忘记**：当前初始化脚本不会重置已有密码，这是防止部署脚本意外接管账号。应走受控的离线恢复流程或备份恢复，不要删除整个用户库。

---

## 参考

- 本仓库：`openai-compatible-agent-integration-guide.md`
- 本仓库：`README.md`、`docs/DESIGN.md`
- 阿里云 ECS/ESSD/安全组官方文档
- Next.js Self-hosting 官方文档
- nginx Reverse Proxy 官方文档
