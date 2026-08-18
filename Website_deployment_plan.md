# Paper Agent 生产部署与版本更新手册

> **适用范围（2026-08-18）**：本手册只描述当前实际采用的部署方式：**单台阿里云北京 ECS（4 vCPU / 16 GiB）+ 公网 IPv4 + 已授权并完成备案/接入条件的子域名 + Nginx HTTPS + FastAPI systemd 服务 + Next.js systemd 服务 + GitHub 私有仓库发布**。
>
> 本手册只保留当前生产服务器实际使用的公网直连部署与 GitHub 更新流程。

---

## 0. 当前生产架构与不可更改的基线

### 0.1 请求路径

```text
浏览器 / 清小搭
  → https://paper-agent.ycr10.cn:443
  → DNS A 记录
  → 阿里云北京 ECS 公网 IPv4
  → Nginx
      ├── /v1/*       → FastAPI 127.0.0.1:8000（清小搭）
      ├── /api/v1/*   → FastAPI 127.0.0.1:8000（自有网页）
      ├── /files/*    → FastAPI 127.0.0.1:8000（导出附件）
      ├── /elements/* → FastAPI 127.0.0.1:8000（图表裁剪）
      ├── /health     → FastAPI 127.0.0.1:8000
      └── /            → Next.js 127.0.0.1:3000
```

### 0.2 固定生产配置

| 项目 | 当前要求 |
|---|---|
| 服务器 | 阿里云北京 ECS，推荐 4 vCPU / 16 GiB、100 GiB 系统盘、4 GiB Swap |
| 系统 | Ubuntu Server 22.04 x86_64 |
| 公网端口 | 22、80、443；其中 22 只允许管理员固定 IP |
| 禁止公网开放 | 3000、8000 |
| 后端 | FastAPI/Uvicorn，**始终只能 1 worker** |
| 前端 | Next.js production build + `next start` |
| 反向代理 | Nginx，HTTPS，SSE 禁止缓冲 |
| 进程管理 | `paper-agent.service`、`paper-agent-web.service` |
| API 清理 | `paper-agent-cleanup.timer` |
| 源码更新 | 本地提交并推送 GitHub，服务器 `git fetch` + fast-forward 更新 |
| 生产目录 | `/opt/paper-agent` |
| 服务账号 | `paper-agent` |
| 模型缓存 | `/var/lib/paper-agent/cache` |

### 0.3 单 worker 与并发说明

必须保留：

```text
--workers 1
```

原因是会话状态、熔断器和限流信号量属于进程内状态，直接增加 worker 会破坏会话一致性。当前代码已把 Docling、SentenceTransformer、CrossEncoder、Chroma、DOCX/图片提取等同步重任务放入进程内单槽后台 worker，因此：

- 清小搭重任务不会再直接占住 FastAPI 事件循环；
- 网页鉴权配置、`/health` 和 SSE 心跳应能继续响应；
- 多个重任务会串行排队，而不是同时抢满内存；
- CPU 仍可能较高，但不应再出现“清小搭一请求，网页完全打不开”的现象。

4C16G 的目标是约 3–6 名混合用户，重型 PDF/OCR/深读建议同一时刻只运行 1 个。需要更高重任务吞吐时应升级架构和容量，而不是增加 Uvicorn worker。

---

## 1. 变量约定

以下命令默认使用当前域名和目录。若实际值不同，先修改变量：

```bash
export DOMAIN='paper-agent.ycr10.cn'
export APP_DIR='/opt/paper-agent'
export APP_USER='paper-agent'
export BRANCH='main'
```

检查：

```bash
printf 'DOMAIN=%s\nAPP_DIR=%s\nAPP_USER=%s\nBRANCH=%s\n' \
  "$DOMAIN" "$APP_DIR" "$APP_USER" "$BRANCH"
```

不要把 DeepSeek Key、VLM Key、`pa_live_...` Agent API Key、管理员密码或 SSH 私钥写进本手册、Git、命令截图或聊天记录。

---

# 第一部分：首次生产部署

## 2. 云服务器和网络

### 2.1 安全组

只保留：

| 协议 | 端口 | 来源 |
|---|---:|---|
| TCP | 22 | 管理员固定公网 IP `/32` |
| TCP | 80 | `0.0.0.0/0`，用于 HTTP 跳转和证书续期 |
| TCP | 443 | `0.0.0.0/0` |

不得给 3000、8000 添加公网安全组规则。它们只监听 `127.0.0.1`，由 Nginx 转发。

### 2.2 DNS 与 HTTPS 前置条件

- `paper-agent.ycr10.cn` 的 A 记录指向当前 ECS 公网 IPv4；
- 父域名已完成 ICP 备案，并满足阿里云接入要求；
- 子域名已获得域名所有者明确授权；
- HTTPS 证书覆盖完整子域名。

验证：

```bash
dig +short A "$DOMAIN"
```

返回值必须包含 ECS 的公网 IPv4。

---

## 3. 安装系统环境

```bash
sudo apt update
sudo apt install -y \
  git curl ca-certificates build-essential nginx \
  software-properties-common poppler-utils tesseract-ocr \
  certbot python3-certbot-nginx dnsutils \
  libgl1 libglib2.0-0

sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
python3.11 --version
```

项目要求 Python 3.11 或更高。不要删除 Ubuntu 自带的系统 Python。

### 3.1 Node.js 和 pnpm

生产固定使用 Node.js 22 LTS 和仓库约定的 pnpm：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh
less /tmp/nodesource_setup.sh
sudo -E bash /tmp/nodesource_setup.sh
sudo apt install -y nodejs

node --version
npm --version
sudo corepack enable --install-directory /usr/local/bin
corepack prepare pnpm@11.9.0 --activate
command -v pnpm
pnpm --version
```

记录 `command -v pnpm` 的实际结果。下文按 `/usr/local/bin/pnpm` 编写；若结果不同，systemd 和构建命令都要替换成实际绝对路径。

### 3.2 服务账号和目录

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/paper-agent \
  --shell /usr/sbin/nologin paper-agent

sudo mkdir -p /opt/paper-agent /var/lib/paper-agent/cache
sudo chown -R paper-agent:paper-agent \
  /opt/paper-agent /var/lib/paper-agent
```

如果账号已存在，`useradd` 报已存在即可，不要删除重建。

### 3.3 配置 4 GiB Swap

先检查：

```bash
swapon --show
free -h
```

如果尚无 Swap：

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || \
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
swapon --show
```

Swap 只用于吸收短时内存峰值，不能替代内存扩容。

---

## 4. 通过 GitHub 首次拉取源码

### 4.1 推荐：GitHub 只读 Deploy Key

为生产服务器配置一个只读 Deploy Key。不要把个人 GitHub 密码或长期 Personal Access Token 留在服务器 shell 历史中。

```bash
sudo -H -u paper-agent mkdir -p /var/lib/paper-agent/.ssh
sudo -H -u paper-agent chmod 700 /var/lib/paper-agent/.ssh
sudo -H -u paper-agent ssh-keygen \
  -t ed25519 \
  -f /var/lib/paper-agent/.ssh/github_deploy_key \
  -C 'paper-agent-production' \
  -N ''

sudo -u paper-agent cat /var/lib/paper-agent/.ssh/github_deploy_key.pub
```

把输出的**公钥**添加到 GitHub 仓库的 Deploy keys，并保持只读。私钥只留在服务器，绝不能复制到仓库、聊天或截图。

创建 SSH 配置：

```bash
sudo -H -u paper-agent tee /var/lib/paper-agent/.ssh/config >/dev/null <<'SSHCONF'
Host github.com
    HostName github.com
    User git
    IdentityFile /var/lib/paper-agent/.ssh/github_deploy_key
    IdentitiesOnly yes
SSHCONF
sudo -H -u paper-agent chmod 600 /var/lib/paper-agent/.ssh/config
```

首次连接前应核对 GitHub SSH 主机指纹，然后写入 `known_hosts`。不要在无法确认指纹时盲目接受主机：

```bash
sudo -H -u paper-agent ssh-keyscan github.com \
  | sudo -H -u paper-agent tee /var/lib/paper-agent/.ssh/known_hosts >/dev/null
sudo -H -u paper-agent chmod 600 /var/lib/paper-agent/.ssh/known_hosts
sudo -H -u paper-agent ssh -T git@github.com
```

GitHub 测试命令通常会提示认证成功但不提供 shell，这是正常现象。

### 4.2 Clone

把仓库地址替换成实际私有仓库：

```bash
sudo -u paper-agent git clone \
  git@github.com:<你的GitHub账号或组织>/<仓库名>.git \
  /opt/paper-agent

cd /opt/paper-agent
sudo -u paper-agent git status --short
sudo -u paper-agent git remote -v
sudo -u paper-agent git branch --show-current
```

生产服务器工作树必须保持干净。不要直接在服务器修改受 Git 管理的源码；所有代码修改都应先在本地提交并推送 GitHub。

---

## 5. 安装 Python 和前端依赖

```bash
cd /opt/paper-agent
sudo -u paper-agent python3.11 -m venv .venv
sudo -H -u paper-agent .venv/bin/pip install --upgrade pip

# CPU-only 服务器必须先安装仓库固定的 CPU 版 PyTorch。
sudo -H -u paper-agent .venv/bin/pip install -r requirements-cpu.txt

# constraints.txt 会由 requirements.txt 引用，避免依赖解析漂移。
sudo -H -u paper-agent .venv/bin/pip install \
  -r requirements.txt -e backend

sudo -H -u paper-agent env HOME=/var/lib/paper-agent \
  bash -c 'cd /opt/paper-agent/frontend && /usr/local/bin/pnpm install --frozen-lockfile'
```

验证 CPU 版 PyTorch 和 Python 依赖：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent .venv/bin/python - <<'PY'
import torch
print("torch:", torch.__version__)
print("cuda:", torch.version.cuda)
assert "+cpu" in torch.__version__, torch.__version__
assert torch.version.cuda is None, torch.version.cuda
PY
sudo -H -u paper-agent .venv/bin/python -m pip check
```

如果 pip 长时间枚举大量 LangChain/LangSmith 版本，立即停止，检查是否确实在项目根目录执行了带 `constraints.txt` 的当前仓库命令。

---

## 6. 运行时数据边界

首次部署只能从 GitHub 拉取源码，不能把开发机的以下内容上传到生产服务器：

- `.env`、SSH 私钥、API Key、证书私钥；
- `data/`、`backend/data/`；
- `history_record/`、`backend/history_record/`；
- PDF、上传文件、SQLite、Chroma、缓存、导出文件；
- `frontend/.next/`、`frontend/node_modules/`、`*.tsbuildinfo`；
- 本地 `.env_conda/`、测试结果、日志和截图。

创建全新的生产运行目录：

```bash
cd /opt/paper-agent
sudo -u paper-agent mkdir -p \
  data/openai_api \
  history_record \
  /var/lib/paper-agent/cache
sudo chmod 700 data data/openai_api history_record \
  /var/lib/paper-agent/cache
```

普通 Git 更新不得删除、覆盖或从本地替换这些生产运行数据。

---

## 7. 本地模型首次预热

Paper Agent 使用两类“模型”：

1. DeepSeek/VLM 等远程 API 模型；
2. 嵌入、重排和 Docling 等服务器本地缓存模型。

首次部署时，本地模型必须用 `paper-agent` 服务账号真正加载一次：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
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
    parser._ensure_converter()
print("embedding, reranker and Docling are ready")
PY
```

首次加载可能下载数百 MiB 到数 GiB。预热完成后才建议在 `.env` 中启用：

```ini
HF_HUB_OFFLINE=1
XDG_CACHE_HOME=/var/lib/paper-agent/cache
```

---

## 8. 生产环境变量

```bash
cd /opt/paper-agent
sudo -u paper-agent cp .env.example .env
sudo chown paper-agent:paper-agent .env
sudo chmod 600 .env
sudo -u paper-agent nano .env
```

最小生产配置示例：

```ini
APP_ENV=production
APP_DEBUG=false
APP_HOST=127.0.0.1
APP_PORT=8000
PUBLIC_BASE_URL=https://paper-agent.ycr10.cn

DEEPSEEK_API_KEY=<填写实际密钥>
DEEPSEEK_BASE_URL=<填写当前已验证的服务地址>
DEEPSEEK_MODEL_LIGHT=<填写当前已验证的模型名>
DEEPSEEK_MODEL_REASONING=<填写当前已验证的模型名>

AUTH_REQUIRED=true
REGISTRATION_OPEN=false
GUEST_ACCESS=false
FRONTEND_ORIGIN=https://paper-agent.ycr10.cn
CORS_ORIGINS=

HF_HUB_OFFLINE=1
XDG_CACHE_HOME=/var/lib/paper-agent/cache

# 可选。留空时多模态功能优雅降级，不影响文本主流程。
MULTIMODAL_BASE_URL=
MULTIMODAL_API_KEY=
MULTIMODAL_MODEL=

OPENAI_API_STORAGE_ROOT=/opt/paper-agent/data/openai_api
OPENAI_API_POLICY_PRESET=balanced
```

注意：

- `PUBLIC_BASE_URL`、`FRONTEND_ORIGIN` 只写 Origin，不带 `/v1`，也不带末尾 `/`；
- Agent API Key 不是 DeepSeek/VLM Key；
- 管理员签发的数据库 Agent Key 不需要写入 `.env`；
- 更新代码时不能用 `.env.example` 覆盖生产 `.env`；
- `.env.example` 新增配置项时，应人工比较并按需补入 `.env`。

### 8.1 只更新生产环境 `.env` 时的正确流程

`.env` 是服务器上的运行时秘密文件，不属于 Git 发布内容。比如更换 `CORE_API_KEY`、`DEEPSEEK_API_KEY` 或第三方 VLM Key 时，**不需要提交 Git、不需要 `git pull`，也不能用 `.env.example` 覆盖生产 `.env`**。必须在云服务器上直接编辑，并重启读取该文件的后端进程。

先登录服务器并确认当前文件权限：

```bash
cd /opt/paper-agent
sudo -u paper-agent test -f .env
sudo stat -c '%U:%G %a %n' .env
```

正常结果应为 `paper-agent:paper-agent 600`。如果不是，先修正：

```bash
sudo chown paper-agent:paper-agent /opt/paper-agent/.env
sudo chmod 600 /opt/paper-agent/.env
```

在修改前建立只允许管理员读取的备份。备份文件不能进入 GitHub：

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/var/lib/paper-agent/backup/env-$STAMP"
sudo install -d -o paper-agent -g paper-agent -m 700 "$BACKUP_DIR"
sudo -u paper-agent cp -a /opt/paper-agent/.env "$BACKUP_DIR/.env"
sudo -u paper-agent chmod 600 "$BACKUP_DIR/.env"
echo "$BACKUP_DIR" | sudo tee /var/lib/paper-agent/last-env-backup-dir
```

使用服务器本地编辑器修改，避免把密钥放进 shell 历史、命令行参数、终端截图或日志：

```bash
sudo -u paper-agent nano /opt/paper-agent/.env
```

例如只修改 CORE 密钥时，在编辑器中找到并替换这一行：

```ini
CORE_API_KEY=在这里粘贴新的 CORE API Key
```

保存后只检查变量“是否存在/非空”，**不要打印变量值**：

```bash
sudo -u paper-agent awk -F= '
  /^[[:space:]]*#/ { next }
  /^[[:space:]]*[A-Z_][A-Z0-9_]*=/ {
    key=$1; sub(/^[[:space:]]+/, "", key); sub(/[[:space:]]+$/, "", key);
    value=substr($0, index($0, "=") + 1);
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", value);
    printf "%s=%s\n", key, (length(value) ? "<configured>" : "<empty>");
  }
' /opt/paper-agent/.env | grep -E '^(CORE_API_KEY|DEEPSEEK_API_KEY|MULTIMODAL_API_KEY)=' || true
```

确认文件权限没有被编辑器改变：

```bash
sudo chown paper-agent:paper-agent /opt/paper-agent/.env
sudo chmod 600 /opt/paper-agent/.env
sudo stat -c '%U:%G %a %n' /opt/paper-agent/.env
```

`EnvironmentFile=/opt/paper-agent/.env` 只在 systemd 服务进程启动时读取。修改 `.env` 后必须重启后端：

```bash
sudo systemctl restart paper-agent
sleep 3
sudo systemctl is-active --quiet paper-agent && echo "paper-agent active"
curl -fsS http://127.0.0.1:8000/health
```

本次只是修改后端 `.env` 时：

- 不需要 `git fetch`、`git merge` 或重新安装 Python 依赖；
- 不需要 `systemctl daemon-reload`，除非同时修改了 `.service` 文件；
- 通常不需要重启 `paper-agent-web`，因为后端 `.env` 由 `paper-agent` 读取；
- 不需要重启 Nginx；
- 如果修改的是前端构建时环境变量（例如 `NEXT_PUBLIC_BACKEND_URL`），则必须重新 build 前端并重启 `paper-agent-web`，不能只重启后端。

以 `CORE_API_KEY` 为例，后端重启后还要在管理员页面 `/admin/paper-search` 检查 CORE：

1. “Key 已配置”状态出现；
2. 如果 CORE 之前被管理员手动关闭，单纯补 Key 不会自动打开它，需要手动打开 CORE 渠道并保存；
3. 执行一次小范围论文检索或连通性检测；
4. 查看日志时只看错误类型和状态码，禁止用会打印环境变量的命令：

```bash
sudo journalctl -u paper-agent -n 100 --no-pager
curl -fsS https://paper-agent.ycr10.cn/health
```

如果更新后服务无法启动，先恢复备份再重启：

```bash
BACKUP_DIR="$(sudo cat /var/lib/paper-agent/last-env-backup-dir)"
sudo -u paper-agent cp -a "$BACKUP_DIR/.env" /opt/paper-agent/.env
sudo chown paper-agent:paper-agent /opt/paper-agent/.env
sudo chmod 600 /opt/paper-agent/.env
sudo systemctl restart paper-agent
sudo systemctl status paper-agent --no-pager
```

不要执行以下危险操作：

```bash
# 禁止：会覆盖生产密钥和其他服务器专属配置
sudo cp .env.example /opt/paper-agent/.env

# 禁止：密钥可能进入 shell history、进程列表或审计日志
export CORE_API_KEY="..."
echo "CORE_API_KEY=..." >> /opt/paper-agent/.env
```

## 8.2 论文平台生产配置与许可门禁

论文平台的生产身份统一放在 `/opt/paper-agent/.env`，不能写入管理员数据库或 Git。推荐至少配置：

```dotenv
PAPER_PLATFORM_CONTACT_EMAIL=真实可联系邮箱
OPENALEX_API_KEY=从 OpenAlex 官方申请的 Key
CORE_LICENSE_CONFIRMED=false
S2_LICENSE_CONFIRMED=false
```

按需配置 `S2_API_KEY`、`CORE_API_KEY`、`NCBI_API_KEY`、`OPENAIRE_CLIENT_ID`、`OPENAIRE_CLIENT_SECRET`。`CORE_LICENSE_CONFIRMED` 和 `S2_LICENSE_CONFIRMED` 只能在运营者核对官方条款、确认当前部署用途符合许可后改为 `true`；只有 API Key 不能绕过许可门禁。逐平台依据见仓库根目录 `Official_Paper_Platform_License_Description.md`。

只检查变量是否配置，不显示内容：

```bash
sudo -u paper-agent awk -F= '
  /^(PAPER_PLATFORM_CONTACT_EMAIL|OPENALEX_API_KEY|S2_API_KEY|S2_LICENSE_CONFIRMED|CORE_API_KEY|CORE_LICENSE_CONFIRMED|NCBI_API_KEY|OPENAIRE_CLIENT_ID|OPENAIRE_CLIENT_SECRET)=/ {
    value=substr($0,index($0,"=")+1);
    printf "%s=%s\n", $1, (length(value) ? "<configured>" : "<empty>")
  }
' /opt/paper-agent/.env
```

保存 `.env` 后必须重启后端，再到 `/admin/paper-search` 核对每个平台的“配置状态、许可状态、运行状态”。OpenAlex 无 Key、CORE/S2 未完成许可确认时显示不可运行属于预期安全行为。

```bash
sudo systemctl restart paper-agent
sleep 3
curl -fsS http://127.0.0.1:8000/health
sudo journalctl -u paper-agent -n 80 --no-pager
```

## 8.3 bioRxiv / medRxiv 官方元数据同步

预印本关键词检索来自官方 metadata API 同步后的本地 SQLite FTS，不抓取网页，也不下载 PDF。数据库位于 `/opt/paper-agent/data/paper_source_catalog.db`。安装每日同步 timer：

```bash
cd /opt/paper-agent
sudo install -o root -g root -m 0644 deploy/systemd/paper-agent-rxiv-sync.service /etc/systemd/system/paper-agent-rxiv-sync.service
sudo install -o root -g root -m 0644 deploy/systemd/paper-agent-rxiv-sync.timer /etc/systemd/system/paper-agent-rxiv-sync.timer
sudo install -d -o paper-agent -g paper-agent -m 0750 /opt/paper-agent/data
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent-rxiv-sync.timer
sudo systemctl start paper-agent-rxiv-sync.service
```

单次双源服务最多运行 15 分钟（脚本为正常收尾预留 30 秒，systemd 另设 15 分钟硬上限）：每个来源先重扫最近 7 天，再从 bioRxiv 2013 年、medRxiv 2019 年开始按日期游标和分页游标继续历史回填。首次回填不阻塞后端，可能需要多个 timer 周期。检查状态：

```bash
sudo systemctl status paper-agent-rxiv-sync.timer --no-pager
sudo systemctl status paper-agent-rxiv-sync.service --no-pager
sudo journalctl -u paper-agent-rxiv-sync.service -n 100 --no-pager
sudo -u paper-agent /opt/paper-agent/.venv/bin/python - <<'PY'
from tools.search.rxiv_catalog import sync_status
for name, row in sync_status().items():
    print(name, "indexed=", row["indexed_count"], "coverage=", row["min_published"], "..", row["max_published"], "cursor=", row["backfill_date"], row["backfill_cursor"], "error=", row["last_error"] or "none")
PY
```

升级或维护前只备份该元数据索引，不把它提交 Git：

```bash
STAMP="$(date +%Y%m%d_%H%M%S)"
sudo install -d -o paper-agent -g paper-agent -m 0700 "/var/backups/paper-agent/$STAMP"
sudo systemctl stop paper-agent-rxiv-sync.service 2>/dev/null || true
sudo -u paper-agent cp -a /opt/paper-agent/data/paper_source_catalog.db* "/var/backups/paper-agent/$STAMP/" 2>/dev/null || true
sudo systemctl start paper-agent-rxiv-sync.timer
```

如果索引损坏，可停止同步、移走 `paper_source_catalog.db*` 后重新启动 timer；数据会从官方 API 重新构建。不要从开发机上传该数据库。

---

## 9. 初始化管理员和清小搭 Agent API Key

### 9.1 初始化管理员

```bash
cd /opt/paper-agent
sudo -u paper-agent .venv/bin/python scripts/bootstrap_administrator.py
```

该脚本是幂等的，不会自动重置现有管理员密码。

### 9.2 创建 Agent API Key

```bash
cd /opt/paper-agent
sudo -u paper-agent .venv/bin/python scripts/create_agent_api_key.py \
  --username administrator \
  --name '清小搭生产接入'
```

完整 `pa_live_...` 只显示一次。立即保存到密码管理器，不要写进 `.env` 或 Git。

---

## 10. 构建 Next.js 前端

`NEXT_PUBLIC_BACKEND_URL` 是构建时变量，必须使用公网 HTTPS Origin：

```bash
cd /opt/paper-agent/frontend
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
```

只要修改了前端源码，或者修改了公网域名，就必须重新执行 `pnpm build`。

---

## 11. systemd 服务

### 11.1 后端：`paper-agent.service`

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

**不得把 `--workers 1` 改成 2、4 或 auto。**

### 11.2 前端：`paper-agent-web.service`

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
Environment=HOME=/var/lib/paper-agent
Environment=BACKEND_URL=http://127.0.0.1:8000
ExecStart=/usr/local/bin/pnpm exec next start -H 127.0.0.1 -p 3000
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

如果 `command -v pnpm` 不是 `/usr/local/bin/pnpm`，替换 `ExecStart` 中的路径。

### 11.3 API 存储清理 timer

```bash
cd /opt/paper-agent
sudo cp deploy/systemd/paper-agent-cleanup.service /etc/systemd/system/
sudo cp deploy/systemd/paper-agent-cleanup.timer /etc/systemd/system/
```

### 11.4 加载并启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent
sleep 3
curl -fsS http://127.0.0.1:8000/health

sudo systemctl enable --now paper-agent-web
sleep 3
curl -I http://127.0.0.1:3000

sudo systemctl enable --now paper-agent-cleanup.timer
sudo systemctl start paper-agent-cleanup.service
sudo systemctl status paper-agent --no-pager
sudo systemctl status paper-agent-web --no-pager
sudo systemctl status paper-agent-cleanup.timer --no-pager
```

核对监听地址：

```bash
sudo ss -ltnp | grep -E ':(3000|8000)\b'
```

必须显示 `127.0.0.1:3000` 和 `127.0.0.1:8000`，不得显示 `0.0.0.0:3000`、`*:3000`、`0.0.0.0:8000` 或 `*:8000`。

---

## 12. Nginx 和 HTTPS

首次部署必须先用纯 HTTP 站点完成证书签发，再切换到包含证书路径的正式 HTTPS 配置。不能在证书文件尚不存在时直接加载 443 配置，否则 `nginx -t` 会失败。

### 12.1 创建 HTTP 引导站点并签发证书

创建 ACME 目录：

```bash
sudo mkdir -p /var/www/paper-agent-acme
sudo chown -R www-data:www-data /var/www/paper-agent-acme
```

先创建 `/etc/nginx/sites-available/paper-agent`，此时只写 80 端口：

```nginx
server {
    listen 80;
    server_name paper-agent.ycr10.cn;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/paper-agent-acme;
        default_type text/plain;
        try_files $uri =404;
    }

    location / {
        default_type text/plain;
        return 200 'Paper Agent HTTPS bootstrap\n';
    }
}
```

启用 HTTP 站点：

```bash
sudo ln -sfn /etc/nginx/sites-available/paper-agent \
  /etc/nginx/sites-enabled/paper-agent
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
curl -i http://paper-agent.ycr10.cn/
```

确认公网 HTTP 已到达本机后签发证书：

```bash
sudo certbot certonly --webroot \
  -w /var/www/paper-agent-acme \
  -d paper-agent.ycr10.cn

sudo test -f /etc/letsencrypt/live/paper-agent.ycr10.cn/fullchain.pem
sudo test -f /etc/letsencrypt/live/paper-agent.ycr10.cn/privkey.pem
```

### 12.2 替换为正式 HTTPS 配置

证书签发成功后，把 `/etc/nginx/sites-available/paper-agent` **完整替换**为：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name paper-agent.ycr10.cn;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/paper-agent-acme;
        default_type text/plain;
        try_files $uri =404;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name paper-agent.ycr10.cn;

    ssl_certificate     /etc/letsencrypt/live/paper-agent.ycr10.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/paper-agent.ycr10.cn/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    client_max_body_size 50m;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header X-Accel-Buffering no always;
    }

    location /api/v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Guest-Id $http_x_guest_id;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header X-Accel-Buffering no always;
    }

    location /files/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Guest-Id $http_x_guest_id;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }

    location /elements/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Guest-Id $http_x_guest_id;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8000/health;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

检查并加载：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I http://paper-agent.ycr10.cn/
curl -i https://paper-agent.ycr10.cn/health
sudo certbot renew --dry-run
```

不要给 `/v1` 或 `/files` 增加 Nginx Basic Auth，否则会破坏清小搭 Bearer 鉴权和附件下载。

---

## 13. 首次上线验收

### 13.1 本机服务

```bash
curl -fsS http://127.0.0.1:8000/health
curl -I http://127.0.0.1:3000
sudo systemctl is-active paper-agent
sudo systemctl is-active paper-agent-web
sudo systemctl is-active nginx
```

### 13.2 公网 HTTPS

```bash
curl -I http://paper-agent.ycr10.cn/
curl -i https://paper-agent.ycr10.cn/health
curl -I https://paper-agent.ycr10.cn/chat
```

期望 HTTP 跳转 HTTPS，`/health` 返回 200，`/chat` 可打开。

### 13.3 清小搭接口

清小搭配置：

```text
Base URL: https://paper-agent.ycr10.cn/v1
API Key: 管理员签发的 pa_live_...
Model: 以 GET /v1/models 返回值为准
```

服务器本机验证 Key，使用隐藏输入避免写入 shell 历史：

```bash
read -rsp '粘贴 pa_live_ Agent API Key: ' KEY; echo
curl -i http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $KEY"
unset KEY
```

### 13.4 清小搭与网页并发验收

在清小搭中发起真实论文检索、深读或 OCR；同时在服务器另一个终端执行：

```bash
while true; do
  date '+%F %T'
  curl -fsS --max-time 2 \
    https://paper-agent.ycr10.cn/api/v1/auth/config
  echo
  curl -fsS --max-time 2 \
    https://paper-agent.ycr10.cn/health
  echo
  sleep 1
done
```

验收标准：

- 两个接口持续快速返回；
- 浏览器 `/chat` 不会无限停在加载动画；
- 清小搭 SSE 能持续输出并以 stop frame、usage、`[DONE]` 正常结束；
- 服务器 CPU 可以升高，但内存不应持续失控；
- 多个本地重任务允许排队，不能因此增加 Uvicorn worker。

监控：

```bash
sudo journalctl -u paper-agent -f
```

另一个终端：

```bash
watch -n 1 'free -h; echo; uptime; echo; ps -eo pid,%cpu,%mem,rss,cmd --sort=-%mem | head -15'
```

检查 OOM：

```bash
sudo dmesg -T | grep -Ei 'oom|out of memory|killed process' || true
```

---

# 第二部分：从 GitHub 更新云服务器 Agent 版本

## 14. 更新原则

生产更新固定使用以下链路：

```text
本地开发和测试
  → git commit
  → git push GitHub
  → 服务器 git fetch
  → 审阅差异
  → 停服务并备份关键数据
  → fast-forward 到 origin/main
  → 按变更安装依赖
  → 按需预热模型
  → 重新构建前端
  → 重启后端和前端
  → 健康、清小搭、网页并发验收
```

禁止：

- 在生产服务器直接编辑源码后再 `git pull`；
- 使用 `git pull --force`；
- 用本地 `data/` 或 `.env` 覆盖服务器；
- 把 `.next`、`node_modules` 上传到服务器；
- 为解决并发问题增加 Uvicorn worker；
- 未验收就删除旧版本记录或备份。

---

## 15. 第一步：本地测试、提交并推送 GitHub

在开发机项目根目录执行：

```bash
git status --short
git diff --check
```

确认没有提交以下内容：

- `.env`、密钥、证书；
- `data/`、`backend/data/`；
- `history_record/`、`backend/history_record/`；
- `.next/`、`node_modules/`；
- PDF、SQLite、Chroma、上传文件和日志；
- 用户自己的 `.zcode/`。

运行后端和前端验证：

```bash
./.env_conda/bin/python -m pytest tests/ -q
cd frontend
pnpm lint
pnpm build
cd ..
```

提交并推送：

```bash
git add <本次需要发布的源码和文档>
git commit -m "fix: describe this release"
git push origin main
```

记录发布 commit：

```bash
git rev-parse HEAD
git log -1 --oneline
```

建议给稳定版本打 tag：

```bash
git tag -a "prod-$(date +%Y%m%d-%H%M)" -m 'Paper Agent production release'
git push origin --tags
```

不要使用 `git add .` 后不检查就直接提交。

---

## 16. 第二步：服务器更新前检查

SSH 登录服务器后：

```bash
cd /opt/paper-agent
sudo -u paper-agent git status --short
sudo -u paper-agent git branch --show-current
sudo -u paper-agent git remote -v
```

`git status --short` 应无输出。若出现源码修改，先查明来源；不要直接覆盖或强制拉取。

如果现有 `origin` 不是当前 GitHub 私有仓库的 SSH 地址，先改为实际地址并测试只读权限：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent git remote set-url origin \
  git@github.com:<你的GitHub账号或组织>/<仓库名>.git
sudo -H -u paper-agent ssh -T git@github.com
sudo -H -u paper-agent git ls-remote --heads origin main
```

记录旧版本：

```bash
cd /opt/paper-agent
OLD_REV="$(sudo -u paper-agent git rev-parse HEAD)"
echo "$OLD_REV" | sudo tee /var/lib/paper-agent/last-production-revision
sudo -u paper-agent git log -1 --oneline
```

检查服务和磁盘：

```bash
sudo systemctl --no-pager --full status paper-agent | head -20
sudo systemctl --no-pager --full status paper-agent-web | head -20
sudo systemctl --no-pager --full status paper-agent-cleanup.timer | head -20
df -h / /opt/paper-agent
free -h
```

拉取远端元数据，但暂不切换代码：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent git fetch --prune origin
sudo -u paper-agent git log --oneline --decorate HEAD..origin/main
sudo -u paper-agent git diff --stat HEAD..origin/main
sudo -u paper-agent git diff --name-only HEAD..origin/main
```

如果 `HEAD..origin/main` 没有内容，服务器已经是最新版本，不需要重启。

---

## 17. 第三步：判断本次更新需要做什么

根据 `git diff --name-only HEAD..origin/main` 判断：

| 变更文件 | 必须执行 |
|---|---|
| 仅 `.py` 后端源码 | 重启 `paper-agent` |
| `frontend/**` | `pnpm install`（锁文件变更时）+ `pnpm build` + 重启 `paper-agent-web` |
| `requirements.txt`、`requirements-cpu.txt`、`constraints.txt`、`backend/pyproject.toml` | 重新安装 Python 依赖，再重启后端 |
| `frontend/package.json`、`frontend/pnpm-lock.yaml` | `pnpm install --frozen-lockfile`，再 build |
| `.env.example`、`core/config.py`、`config/settings.yaml` | 人工比较生产 `.env` 和配置；不得覆盖 `.env` |
| 仅服务器 `.env`（例如更新 `CORE_API_KEY`） | 服务器上备份并编辑 `.env`，校验权限，重启 `paper-agent`，执行健康/真实渠道验证；不需要 Git、依赖安装或 `daemon-reload` |
| `deploy/systemd/**` | 复制更新后的 unit，`systemctl daemon-reload`，按需重启 timer/service |
| Nginx 配置说明或公网路径变化 | 人工修改 Nginx，`nginx -t` 后 reload |
| 本地模型名称或 Docling/torch/sentence-transformers 版本变化 | 在启动前重新预热相关模型 |
| 数据库 schema 相关代码 | 必须保留更新前数据库备份并重点检查启动日志 |
| 仅 Markdown 文档 | 通常不需要重启服务 |

### 17.1 本次“网页被清小搭请求阻塞”修复需要什么

当前这批修复包含后端异步卸载、前端鉴权超时和页面代码，因此部署时需要：

1. 拉取新代码；
2. 若依赖文件没有变化，不需要重装 Python 或 Node 依赖；
3. 重新执行 `pnpm build`；
4. 重启 `paper-agent`；
5. 重启 `paper-agent-web`；
6. 不需要重新下载 DeepSeek/VLM 模型；
7. 本地 embedding/reranker/Docling 缓存未变时不需要重新下载。

---

## 18. 第四步：进入维护窗口并备份

建议先通知用户进入短维护窗口。停止前端和后端，避免更新期间继续写数据库或使用正在变化的 `.next`：

```bash
sudo systemctl stop paper-agent-web
sudo systemctl stop paper-agent
```

确认已停止：

```bash
sudo systemctl is-active paper-agent || true
sudo systemctl is-active paper-agent-web || true
```

创建带时间戳的备份目录：

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/var/lib/paper-agent/backup/$STAMP"
sudo mkdir -p "$BACKUP_DIR"
sudo chown paper-agent:paper-agent "$BACKUP_DIR"
echo "$BACKUP_DIR" | sudo tee /var/lib/paper-agent/last-backup-dir
echo "BACKUP_DIR=$BACKUP_DIR"
```

备份 `.env` 和关键 SQLite 文件：

```bash
cd /opt/paper-agent
sudo -u paper-agent cp -a .env "$BACKUP_DIR/.env"

sudo -H -u paper-agent bash -c '
  set -eu
  cd /opt/paper-agent
  out="$1"
  find data -type f \
    \( -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" \) \
    -exec cp --parents -a {} "$out" \;
' bash "$BACKUP_DIR"
```

如果本次明确涉及 Chroma、上传或历史格式变更，并且磁盘空间允许，再做完整运行数据备份：

```bash
sudo -H -u paper-agent tar -C /opt/paper-agent \
  -czf "$BACKUP_DIR/runtime-data.tar.gz" \
  data history_record
```

完整备份可能较大。普通代码更新至少保留 `.env` 和数据库备份；不要把生产备份提交 GitHub。

---

## 19. 第五步：把服务器源码更新到 GitHub 版本

重新读取旧版本号，避免更换终端后变量丢失：

```bash
OLD_REV="$(sudo cat /var/lib/paper-agent/last-production-revision)"
cd /opt/paper-agent
sudo -H -u paper-agent git checkout main
sudo -H -u paper-agent git merge --ff-only origin/main
sudo -u paper-agent git log -1 --oneline
sudo -u paper-agent git status --short
```

记录新版本：

```bash
NEW_REV="$(sudo -u paper-agent git rev-parse HEAD)"
echo "OLD_REV=$OLD_REV"
echo "NEW_REV=$NEW_REV"
```

`git merge --ff-only` 如果失败，应停止更新并调查分叉原因，不要使用 `git reset --hard origin/main` 掩盖问题。

确认运行数据仍然存在：

```bash
sudo -u paper-agent test -f /opt/paper-agent/.env && echo '.env preserved'
sudo -u paper-agent test -d /opt/paper-agent/data && echo 'data preserved'
sudo -u paper-agent test -d /opt/paper-agent/history_record && echo 'history preserved'
```

---

## 20. 第六步：按需更新依赖

先重新读取旧版本号，再查看旧版到新版的依赖文件差异：

```bash
OLD_REV="$(sudo cat /var/lib/paper-agent/last-production-revision)"
cd /opt/paper-agent
sudo -u paper-agent git diff --name-only "$OLD_REV"..HEAD -- \
  requirements-cpu.txt requirements.txt constraints.txt \
  backend/pyproject.toml \
  frontend/package.json frontend/pnpm-lock.yaml
```

### 20.1 Python 依赖有变化时

```bash
cd /opt/paper-agent
sudo -H -u paper-agent .venv/bin/pip install --upgrade pip
sudo -H -u paper-agent .venv/bin/pip install -r requirements-cpu.txt
sudo -H -u paper-agent .venv/bin/pip install \
  -r requirements.txt -e backend
sudo -H -u paper-agent .venv/bin/python -m pip check
```

若依赖文件没有变化，通常不需要重复安装 Python 包。`backend` 使用 editable install，普通 Python 源码更新会直接由重启后的服务加载。

再次确认 CPU 版 torch：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent .venv/bin/python - <<'PY'
import torch
print(torch.__version__, torch.version.cuda)
assert "+cpu" in torch.__version__
assert torch.version.cuda is None
PY
```

### 20.2 前端依赖有变化时

```bash
cd /opt/paper-agent/frontend
sudo -H -u paper-agent env HOME=/var/lib/paper-agent \
  /usr/local/bin/pnpm install --frozen-lockfile
```

若 `package.json` 和 `pnpm-lock.yaml` 均未变化，可以跳过 `pnpm install`，但只要前端源码变化，仍必须重新 build。

### 20.3 systemd 文件有变化时

仓库目前直接提供清理服务 unit：

```bash
cd /opt/paper-agent
sudo cp deploy/systemd/paper-agent-cleanup.service /etc/systemd/system/
sudo cp deploy/systemd/paper-agent-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

后端和前端 unit 若按本手册人工维护，应对照新版手册检查 `ExecStart`、`EnvironmentFile`、`ReadWritePaths` 和 `--workers 1`。修改任何 unit 后都要执行：

```bash
sudo systemctl daemon-reload
```

---

## 21. 模型到底要不要重新启动或下载

### 21.1 结论表

| 模型/组件 | 更新后是否单独重启 | 是否重新下载 |
|---|---|---|
| DeepSeek 远程文本模型 | 不需要单独模型服务；重启 `paper-agent` 即可重新读取代码和 `.env` | 不下载，本机只调用远程 API |
| 第三方 VLM | 不需要单独模型服务；重启 `paper-agent` 即可 | 不下载，本机只调用远程 API |
| SentenceTransformer embedding | 随 `paper-agent` 进程停止和重新加载 | 缓存和模型名未变时不下载 |
| CrossEncoder reranker | 随 `paper-agent` 进程停止和重新加载 | 缓存和模型名未变时不下载 |
| Docling layout/OCR/table 模型 | 随 `paper-agent` 进程停止和懒加载 | 缓存和依赖未变时不下载 |
| Chroma | 不是独立模型服务 | 不下载；读取现有生产索引 |

因此，正常代码更新不存在额外的 `systemctl restart model` 命令。后端模型对象在 `paper-agent` 进程内，执行：

```bash
sudo systemctl restart paper-agent
```

就已经完成“后端和本地模型进程重启”。第一次检索/深读可能因为懒加载比后续请求慢，这是正常冷启动。

### 21.2 只有以下情况需要重新预热

- `config/settings.yaml` 中 embedding/reranker 模型名发生变化；
- Docling、torch、sentence-transformers 或相关模型依赖发生升级；
- `/var/lib/paper-agent/cache` 被清空、损坏或迁移；
- 日志明确显示离线模式找不到模型文件；
- 更换了服务器。

重新预热：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  XDG_CACHE_HOME=/var/lib/paper-agent/cache \
  HF_HUB_OFFLINE=0 \
  HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python - <<'PY'
from core.embeddings import get_embedder
from tools.retrieval.rerank import get_reranker
from tools.pdf.structure import get_structure_parser

assert get_embedder() is not None
assert get_reranker() is not None
parser = get_structure_parser()
if hasattr(parser, "_ensure_converter"):
    parser._ensure_converter()
print("local models are ready")
PY
```

预热完成后仍使用生产 `.env` 中的 `HF_HUB_OFFLINE=1` 启动服务。不要删除 `/var/lib/paper-agent/cache` 来“强制更新”，除非已经确认缓存损坏并预留了重新下载时间和空间。

---

## 22. 第七步：检查生产配置变化

比较 `.env.example`：

```bash
cd /opt/paper-agent
sudo -u paper-agent git diff "$OLD_REV"..HEAD -- .env.example
```

如果新增环境变量，人工编辑生产 `.env`：

```bash
sudo -u paper-agent nano /opt/paper-agent/.env
sudo chmod 600 /opt/paper-agent/.env
sudo chown paper-agent:paper-agent /opt/paper-agent/.env
```

不得执行：

```bash
# 错误示例：会覆盖生产密钥和配置
cp .env.example .env
```

核对关键非敏感项时不要打印整个 `.env`：

```bash
sudo -u paper-agent grep -E \
  '^(APP_ENV|APP_DEBUG|APP_HOST|APP_PORT|PUBLIC_BASE_URL|AUTH_REQUIRED|REGISTRATION_OPEN|GUEST_ACCESS|FRONTEND_ORIGIN|HF_HUB_OFFLINE|XDG_CACHE_HOME|OPENAI_API_STORAGE_ROOT)=' \
  /opt/paper-agent/.env
```

---

## 23. 第八步：重新构建前端

只要 `frontend/**` 有源码变化，就执行：

```bash
cd /opt/paper-agent/frontend
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
```

构建必须成功后才能启动 `paper-agent-web`。域名写错的典型表现是网页能打开，但对话 SSE 请求连接到错误地址。

如果本次只更新后端且 `frontend/**` 没有变化，可以跳过前端 build 和前端重启；不过完整发布窗口中统一重建可以减少版本不一致风险。

---

## 24. 第九步：启动服务

先启动后端：

```bash
sudo systemctl start paper-agent
sleep 3
sudo systemctl status paper-agent --no-pager
curl -fsS --max-time 5 http://127.0.0.1:8000/health
```

后端健康后启动前端：

```bash
sudo systemctl start paper-agent-web
sleep 3
sudo systemctl status paper-agent-web --no-pager
curl -I --max-time 5 http://127.0.0.1:3000
```

如果 systemd unit 有修改：

```bash
sudo systemctl daemon-reload
```

如果 Nginx 配置没有变化，不需要重启 Nginx。如果改过 Nginx：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

如果清理 timer 的 unit 没有变化，不需要重启 timer；脚本代码会在下一次 timer 触发时使用新版本。若 unit 有变化：

```bash
sudo systemctl restart paper-agent-cleanup.timer
sudo systemctl status paper-agent-cleanup.timer --no-pager
```

---

## 25. 第十步：更新后验收

### 25.1 版本和工作树

```bash
cd /opt/paper-agent
sudo -u paper-agent git log -1 --oneline
sudo -u paper-agent git status --short
```

工作树应无输出；生产运行数据因被 `.gitignore` 排除，不应出现在 Git 状态中。

### 25.2 服务与端口

```bash
sudo systemctl is-active paper-agent
sudo systemctl is-active paper-agent-web
sudo systemctl is-active nginx
sudo systemctl is-active paper-agent-cleanup.timer
sudo ss -ltnp | grep -E ':(3000|8000)\b'
```

### 25.3 日志

```bash
sudo journalctl -u paper-agent -n 100 --no-pager
sudo journalctl -u paper-agent-web -n 100 --no-pager
sudo journalctl -u paper-agent-cleanup.service -n 50 --no-pager
```

快速筛查：

```bash
sudo journalctl -u paper-agent -n 200 --no-pager \
  | grep -Ei 'error|traceback|exception|schema|permission denied|out of memory' \
  || echo '后端近期日志未发现目标错误'
```

### 25.4 健康和网页

```bash
curl -fsS --max-time 5 http://127.0.0.1:8000/health
curl -fsS --max-time 5 https://paper-agent.ycr10.cn/health
curl -fsS --max-time 5 https://paper-agent.ycr10.cn/api/v1/auth/config
curl -I --max-time 5 https://paper-agent.ycr10.cn/chat
```

### 25.5 清小搭 Key 和接口

```bash
read -rsp '粘贴 pa_live_ Agent API Key: ' KEY; echo
curl -i --max-time 10 \
  https://paper-agent.ycr10.cn/v1/models \
  -H "Authorization: Bearer $KEY"
unset KEY
```

### 25.6 真实业务验收

逐项完成：

- [ ] 浏览器登录和 `/chat` 首屏正常；
- [ ] 普通文字对话可流式输出；
- [ ] 管理员页面 `/admin/agent-keys`、`/admin/display-policy` 可打开；
- [ ] 清小搭可以完成最小对话；
- [ ] 清小搭工具请求能展示 markdown 卡片、技能加载提示和附件；
- [ ] 上传一个文件并确认权限与预览正常；
- [ ] 发起真实论文检索或深读；
- [ ] 清小搭重任务期间 `/api/v1/auth/config` 和 `/health` 仍在 2 秒内返回；
- [ ] `free -h` 没有持续内存耗尽；
- [ ] `dmesg` 没有 OOM kill。

### 25.7 当前并发问题修复的专门验收

终端 A 持续检测：

```bash
while true; do
  printf '%s auth=' "$(date '+%F %T')"
  curl -fsS -o /dev/null -w '%{http_code} %{time_total}s' \
    --max-time 2 \
    https://paper-agent.ycr10.cn/api/v1/auth/config || printf 'FAILED'
  printf ' health='
  curl -fsS -o /dev/null -w '%{http_code} %{time_total}s' \
    --max-time 2 \
    https://paper-agent.ycr10.cn/health || printf 'FAILED'
  echo
  sleep 1
done
```

终端 B：

```bash
sudo journalctl -u paper-agent -f
```

然后在清小搭中触发检索、深读或 OCR。预期终端 A 持续返回 HTTP 200；若大量超时，先确认：

```bash
cd /opt/paper-agent
sudo -u paper-agent test -f core/blocking.py && echo 'blocking worker code exists'
sudo systemctl show paper-agent -p MainPID -p ExecStart
sudo journalctl -u paper-agent -n 100 --no-pager
```

不要通过增加 worker 规避问题。

---

## 26. 更新失败时回滚

### 26.1 代码回滚

先停服务：

```bash
sudo systemctl stop paper-agent-web paper-agent
```

读取旧版本：

```bash
OLD_REV="$(sudo cat /var/lib/paper-agent/last-production-revision)"
echo "$OLD_REV"
```

确认服务器没有需要保留的源码修改后回滚：

```bash
cd /opt/paper-agent
sudo -u paper-agent git status --short
sudo -H -u paper-agent git reset --hard "$OLD_REV"
sudo -u paper-agent git log -1 --oneline
```

`git reset --hard` 只允许用于工作树已经确认干净的生产源码目录。它不应删除被 Git 忽略的 `data/`、`history_record/` 和 `.env`，但仍必须提前完成第 18 节备份。

### 26.2 恢复旧版依赖和前端

回滚后，按旧版本文件重新安装依赖：

```bash
cd /opt/paper-agent
sudo -H -u paper-agent .venv/bin/pip install -r requirements-cpu.txt
sudo -H -u paper-agent .venv/bin/pip install \
  -r requirements.txt -e backend
sudo -H -u paper-agent .venv/bin/python -m pip check

cd /opt/paper-agent/frontend
sudo -H -u paper-agent env HOME=/var/lib/paper-agent \
  /usr/local/bin/pnpm install --frozen-lockfile
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
```

### 26.3 数据库只在确有 schema 不兼容时恢复

普通代码错误不应立即覆盖生产数据。只有日志明确显示旧代码无法读取新版 schema，才从第 18 节备份恢复数据库。恢复前再次复制当前故障现场：

```bash
sudo systemctl stop paper-agent
sudo cp -a /opt/paper-agent/data \
  "/var/lib/paper-agent/failed-data-$(date +%Y%m%d-%H%M%S)"
```

然后根据实际 `BACKUP_DIR` 有选择地恢复对应数据库，不要整目录盲目覆盖：

```bash
# 示例；先 ls 检查实际备份结构，再替换 <BACKUP_DIR>。
sudo find <BACKUP_DIR>/data -maxdepth 4 -type f -print
```

API 侧 `data/openai_api/state.db` 可再生，但删除它会丢失清小搭的短期 checkpoint/续接状态。因此优先恢复备份，只有确认不需要续接状态时才考虑删除重建。

### 26.4 重启并重新验收

```bash
sudo systemctl start paper-agent
sleep 3
curl -fsS http://127.0.0.1:8000/health
sudo systemctl start paper-agent-web
sudo systemctl status paper-agent paper-agent-web --no-pager
```

然后重新执行第 25 节。

---

## 27. 常见更新问题

| 现象 | 原因与处理 |
|---|---|
| `git fetch` 认证失败 | 检查 GitHub Deploy Key、`/var/lib/paper-agent/.ssh/config`、仓库权限和 `sudo -H -u paper-agent ssh -T git@github.com` |
| `git merge --ff-only` 失败 | 服务器源码被改动或远端历史被改写；停止更新，先在本地整理提交，不要强制 pull |
| 拉取后 `.env` 不见了 | 不应发生；`.env` 必须被 Git 忽略。立即停服务，从第 18 节备份恢复并审计 `.gitignore` |
| pip 拉取 CUDA/NVIDIA 大包 | 没先安装 `requirements-cpu.txt`，或依赖约束变化；停止并核对 CPU 安装顺序 |
| pip 长时间依赖回溯 | 确认使用当前仓库 `constraints.txt` 和根目录命令 |
| `pnpm build` 失败或 OOM | 保持后端和前端停止，确认 4 GiB Swap、磁盘和内存；检查构建日志，不要直接启动旧/半成品 `.next` |
| 网页打开但一直加载 | 检查 `/api/v1/auth/config`、前端 build 是否成功、`paper-agent-web` 是否是新版本；当前前端有 8 秒配置请求超时，不应永久 spinner |
| 网页能打开但对话连接错误 | build 时 `NEXT_PUBLIC_BACKEND_URL` 错误，按第 23 节重新构建 |
| `/health` 正常但清小搭 401 | Agent API Key 无效、被撤销或粘贴错误；重新用隐藏输入验证 `/v1/models` |
| 清小搭重任务时网页再次阻塞 | 确认新代码包含 `core/blocking.py` 且后端已重启；检查日志和 OOM；不要增加 worker |
| 服务报 Permission denied | 新版本写入了 systemd `ReadWritePaths` 之外的目录；核对设计和 unit，必要时回滚，不能简单放开整个文件系统 |
| 本地模型离线加载失败 | 缓存缺失或模型名变化；按第 21.2 节在维护窗口重新预热 |
| 更新后第一次请求较慢 | embedding、reranker、Docling 在新后端进程内懒加载，缓存存在时属于正常冷启动 |
| Nginx 502 | 检查 `paper-agent`/`paper-agent-web` 状态和 127.0.0.1:8000/3000 监听 |
| SSE 一段时间后断开 | 检查 Nginx `proxy_buffering off`、`proxy_read_timeout 3600s` 和上游日志 |

---

## 28. 日常运维命令速查

### 服务状态

```bash
sudo systemctl status paper-agent --no-pager
sudo systemctl status paper-agent-web --no-pager
sudo systemctl status nginx --no-pager
sudo systemctl status paper-agent-cleanup.timer --no-pager
```

### 重启

```bash
sudo systemctl restart paper-agent
sudo systemctl restart paper-agent-web
```

只更新生产 `.env`（例如 CORE API Key）时，不要执行 Git 更新；按第 8.1 节备份、编辑并只重启 `paper-agent`。

正常代码更新不需要重启 Nginx；只有 Nginx 配置变化时：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 日志

```bash
sudo journalctl -u paper-agent -f
sudo journalctl -u paper-agent-web -f
sudo journalctl -u nginx -n 100 --no-pager
```

### 健康检查

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS https://paper-agent.ycr10.cn/health
curl -fsS https://paper-agent.ycr10.cn/api/v1/auth/config
```

### 资源

```bash
free -h
df -h
uptime
ps -eo pid,%cpu,%mem,rss,cmd --sort=-%mem | head -15
sudo dmesg -T | grep -Ei 'oom|out of memory|killed process' || true
```

### 证书

```bash
sudo certbot certificates
sudo certbot renew --dry-run
```

### 清理 timer

```bash
sudo systemctl list-timers paper-agent-cleanup.timer
sudo journalctl -u paper-agent-cleanup.service -n 100 --no-pager
```

---

## 29. 发布完成清单

- [ ] 本地测试、lint、build 已通过；
- [ ] 本地只提交源码和文档，没有运行时数据或密钥；
- [ ] 新 commit 已推送 GitHub；
- [ ] 服务器更新前工作树干净；
- [ ] 已记录 `OLD_REV`；
- [ ] 已备份 `.env` 和关键数据库；
- [ ] 使用 `git fetch` + `git merge --ff-only origin/main` 更新；
- [ ] Python/Node 依赖仅在对应文件变化时更新；
- [ ] 本地模型仅在模型名、依赖或缓存变化时重新预热；
- [ ] 前端源码变化后已重新 `pnpm build`；
- [ ] 后端仍为单 Uvicorn worker；
- [ ] 后端健康后再启动前端；
- [ ] Nginx 配置未变时只保持运行，不做无意义重启；
- [ ] `/health`、`/api/v1/auth/config`、`/chat` 均正常；
- [ ] 清小搭 `/v1/models` 和真实工具请求正常；
- [ ] 清小搭重任务期间网页与健康接口仍快速响应；
- [ ] 日志无 traceback、权限错误、schema 错误和 OOM；
- [ ] 旧 commit 和备份保留到新版本稳定后再按运维策略清理。

---

## 30. 当前版本专属云服务器更新步骤

> **状态：已于 2026-08-18 获得用户对当前版本上云更新的明确确认。**
>
> 本节只发布“清小搭文件输入协议与 API 上传策略”版本。应用代码目标固定为
> `af3bf6dca40c634b8de86ba3439255c49d481557`，不得使用无版本边界的 `git pull` 或直接跟随未来的
> `origin/main`。用户已于 2026-08-18 从生产服务器 shell 直接确认当前 HEAD 为
> `63a0799997fc7dc94fb0e140214d35b0a749b44b`，SSH 登录用户为 `ecs-user`。服务器预检仍必须在停服务前
> 再次读取 HEAD 并做硬断言；HEAD、分支、remote 或工作树任一项不符，就立即中止，不能猜测或强制更新。
>
> SSH 主机是 `paper-agent.ycr10.cn`（公网 IPv4 `123.57.6.126`），登录用户是 `ecs-user`。所有“服务器端”
> 命令都在同一个具有 `sudo` 权限的 SSH shell 中执行。

### 30.1 版本边界、真实差异和发布影响

```text
生产旧 revision（服务器必须现场确认）：63a0799997fc7dc94fb0e140214d35b0a749b44b
应用目标 revision：                    af3bf6dca40c634b8de86ba3439255c49d481557
分支：                                 main
GitHub remote：                        git@github.com:invincible-summer/Paper-Agent.git
生产目录 / 服务账号：                  /opt/paper-agent / paper-agent
生产域名 / Origin：                    https://paper-agent.ycr10.cn
后端 / 前端：                          127.0.0.1:8000 / 127.0.0.1:3000
systemd：                              paper-agent.service
                                      paper-agent-web.service
                                      paper-agent-cleanup.timer
                                      paper-agent-rxiv-sync.timer
Nginx site：                           /etc/nginx/sites-available/paper-agent
```

旧 revision 到目标 revision 修改 21 个文件（556 行新增、63 行删除），涉及 `/v1` 文件 URL 与
`file_id` 兼容、API 私有附件/checkpoint、200 MiB 管理员策略、DOC/XLS/XLSX `deferred` 通道、
deep-read/RAG 降级、`/admin/api-storage` 前端和相应文档/测试。

| 项目 | 真实 diff 结果 | 本次动作 |
|---|---|---|
| Python 依赖 | `requirements*.txt`、constraints、Python manifest/lock 未变 | **不执行 pip install** |
| Node 依赖 | frontend package/lock 未变 | `node_modules` 存在时**不执行 pnpm install** |
| `.env` / 配置 | `.env.example`、`config/**` 未变 | 不改 `.env`，只核对权限和非敏感变量名 |
| 本地模型 | 模型名、模型配置、模型依赖未变 | 不下载、不预热；缓存缺失时才条件预热 |
| systemd | `deploy/systemd/**` 未变 | 不覆盖 unit，不执行 `daemon-reload` |
| Nginx | 路由和配置未变 | 只执行 `nginx -t`，不 reload |
| 前端 | 管理员 API storage 页面有变化 | 必须用真实 Origin 重新 build |
| 数据库 | `data/openai_api/state.db` schema **v5 → v6** | 后端启动自动迁移；回滚必须恢复升级前 `state.db*` |

预计维护窗口 **3–8 分钟**。备份范围：生产 `.env`、完整 `data/`、完整 `history_record/`、当前
systemd/Nginx 配置，以及单独可快速恢复的 `data/openai_api/state.db*`。备份只留在云服务器，禁止
复制进 Git 或开发机。

### 30.2 开发机最终验证和 GitHub 推送

```bash
cd /home/invincible/daily/vibecoding/Agent_Develop/Paper_Agent
export OLD_REV='63a0799997fc7dc94fb0e140214d35b0a749b44b'
export TARGET_REV='af3bf6dca40c634b8de86ba3439255c49d481557'

test "$(git branch --show-current)" = main
test "$(git remote get-url origin)" = \
  'git@github.com:invincible-summer/Paper-Agent.git'
git cat-file -e "$OLD_REV^{commit}"
git cat-file -e "$TARGET_REV^{commit}"
git merge-base --is-ancestor "$OLD_REV" "$TARGET_REV"
test "$(git show -s --format=%P "$TARGET_REV")" = "$OLD_REV"
git diff --check
git show --stat --oneline "$TARGET_REV"
git diff --name-status "$OLD_REV..$TARGET_REV"

./.env_conda/bin/python -m pytest \
  tests/test_api_storage_foundation.py \
  tests/test_admin_api_storage.py \
  tests/test_openai_compat.py \
  tests/test_attachment_persist.py \
  tests/test_attachment_multimodal.py -q
./.env_conda/bin/python -m pytest tests/ -q

cd frontend
pnpm lint
BACKEND_URL=http://127.0.0.1:8000 \
NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
pnpm build
cd ..

git diff --check
test -z "$(git ls-files | grep -E \
  '(^|/)(data|backend/data|history_record|backend/history_record)/|(^|/)\.env$|\.pem$|\.key$' \
  || true)"
git status --short --branch
git push origin main
git fetch origin main
git cat-file -e "$TARGET_REV^{commit}"
git merge-base --is-ancestor "$TARGET_REV" origin/main
```

部署手册可能作为目标应用 commit 之后的文档 commit 推送，因此服务器只要求 `$TARGET_REV` 是
`origin/main` 的祖先；生产实际运行版本仍精确停在 `$TARGET_REV`。

### 30.3 SSH 登录和只读预检

开发机使用已确认的生产 SSH 用户登录；不要把密码或私钥写进命令历史：

```bash
ssh ecs-user@paper-agent.ycr10.cn
```

登录后执行：

```bash
set -euo pipefail
export OLD_REV='63a0799997fc7dc94fb0e140214d35b0a749b44b'
export TARGET_REV='af3bf6dca40c634b8de86ba3439255c49d481557'
export APP_DIR='/opt/paper-agent'
export APP_USER='paper-agent'
export DOMAIN='paper-agent.ycr10.cn'
export RELEASE_ID='20260818-qxd-file-input-af3bf6d'
export BACKUP_DIR="/var/backups/paper-agent/$RELEASE_ID"

cd "$APP_DIR"
CURRENT_REV="$(sudo -H -u "$APP_USER" git rev-parse HEAD)"
printf 'current=%s\nexpected-old=%s\ntarget=%s\n' \
  "$CURRENT_REV" "$OLD_REV" "$TARGET_REV"
test "$CURRENT_REV" = "$OLD_REV" || {
  echo "ABORT: server HEAD=$CURRENT_REV, expected $OLD_REV"; exit 1;
}
test "$(sudo -H -u "$APP_USER" git branch --show-current)" = main || {
  echo 'ABORT: production branch is not main'; exit 1;
}
test "$(sudo -H -u "$APP_USER" git remote get-url origin)" = \
  'git@github.com:invincible-summer/Paper-Agent.git' || {
  echo 'ABORT: production origin mismatch'; exit 1;
}
test -z "$(sudo -H -u "$APP_USER" git status --porcelain)" || {
  echo 'ABORT: production worktree is not clean';
  sudo -H -u "$APP_USER" git status --short; exit 1;
}

sudo systemctl is-active paper-agent
sudo systemctl is-active paper-agent-web
sudo systemctl is-active paper-agent-cleanup.timer
sudo systemctl is-active paper-agent-rxiv-sync.timer
sudo systemctl is-active nginx
sudo systemctl show paper-agent -p ExecStart -p User -p Group -p WorkingDirectory
sudo systemctl show paper-agent-web -p ExecStart -p User -p Group -p WorkingDirectory
sudo ss -ltnp | grep -E '127\.0\.0\.1:(3000|8000)\b'
df -h / "$APP_DIR" /var/backups
free -h
swapon --show
```

`paper-agent.service` 必须仍只有一个 Uvicorn worker。资源不足时先释放空间或扩容，不要继续。

### 30.4 fetch 和真实 diff 复核

```bash
cd "$APP_DIR"
sudo -H -u "$APP_USER" git fetch --prune origin
sudo -H -u "$APP_USER" git cat-file -e "$TARGET_REV^{commit}"
sudo -H -u "$APP_USER" git merge-base --is-ancestor "$OLD_REV" "$TARGET_REV"
sudo -H -u "$APP_USER" git merge-base --is-ancestor \
  "$TARGET_REV" "$(sudo -H -u "$APP_USER" git rev-parse origin/main)"
sudo -H -u "$APP_USER" git diff --stat "$OLD_REV..$TARGET_REV"
sudo -H -u "$APP_USER" git diff --name-status "$OLD_REV..$TARGET_REV"

# 前两条必须无输出；最后一条应显示两个前端源码文件。
test -z "$(sudo -H -u "$APP_USER" git diff --name-only \
  "$OLD_REV..$TARGET_REV" -- \
  requirements.txt requirements-cpu.txt constraints.txt pyproject.toml \
  poetry.lock uv.lock package.json pnpm-lock.yaml \
  frontend/package.json frontend/pnpm-lock.yaml)"
test -z "$(sudo -H -u "$APP_USER" git diff --name-only \
  "$OLD_REV..$TARGET_REV" -- \
  .env.example config deploy/systemd deploy/nginx nginx \
  Dockerfile docker-compose.yml)"
sudo -H -u "$APP_USER" git diff --name-only \
  "$OLD_REV..$TARGET_REV" -- frontend
```

结果与预期不符就停止并重新审计，不要套用本节。

### 30.5 停服务并备份生产数据

```bash
sudo systemctl stop paper-agent-web
sudo systemctl stop paper-agent
sudo systemctl stop paper-agent-cleanup.timer
sudo systemctl stop paper-agent-rxiv-sync.timer
sudo systemctl stop paper-agent-cleanup.service paper-agent-rxiv-sync.service || true
sudo systemctl is-active paper-agent-web && exit 1 || true
sudo systemctl is-active paper-agent && exit 1 || true

sudo install -d -o root -g root -m 0700 "$BACKUP_DIR"
sudo install -d -o root -g root -m 0700 "$BACKUP_DIR/state-db-v5"
sudo sh -c "printf '%s\n' \
  'release_id=$RELEASE_ID' \
  'old_rev=$OLD_REV' \
  'target_rev=$TARGET_REV' \
  'created_at_utc='\"\$(date -u +%FT%TZ)\" \
  > '$BACKUP_DIR/release-meta.txt'"

sudo tar --numeric-owner --acls --xattrs -czf \
  "$BACKUP_DIR/runtime-env-data-history.tar.gz" \
  -C "$APP_DIR" .env data history_record
sudo tar --numeric-owner --acls --xattrs -czf \
  "$BACKUP_DIR/system-config.tar.gz" -C / \
  etc/systemd/system/paper-agent.service \
  etc/systemd/system/paper-agent-web.service \
  etc/systemd/system/paper-agent-cleanup.service \
  etc/systemd/system/paper-agent-cleanup.timer \
  etc/systemd/system/paper-agent-rxiv-sync.service \
  etc/systemd/system/paper-agent-rxiv-sync.timer \
  etc/nginx/sites-available/paper-agent
sudo find "$APP_DIR/data/openai_api" -maxdepth 1 -type f \
  -name 'state.db*' -exec cp -a -t "$BACKUP_DIR/state-db-v5" {} +

sudo sh -c "cd '$BACKUP_DIR' && \
  find . -type f ! -name SHA256SUMS -print0 | sort -z | \
  xargs -0 sha256sum > SHA256SUMS"
sudo tar -tzf "$BACKUP_DIR/runtime-env-data-history.tar.gz" >/dev/null
sudo tar -tzf "$BACKUP_DIR/system-config.tar.gz" >/dev/null
sudo sh -c "cd '$BACKUP_DIR' && sha256sum -c SHA256SUMS"
sudo du -sh "$BACKUP_DIR"
```

不要 `cat .env`，不要查看或上传备份内容。完整 tar、独立 `state.db*` 或校验失败时不得继续。

### 30.6 精确 fast-forward 到目标 commit

```bash
cd "$APP_DIR"
sudo sh -c "printf '%s\n' '$OLD_REV' > /var/lib/paper-agent/last-production-revision"
sudo -H -u "$APP_USER" git switch main
sudo -H -u "$APP_USER" git merge --ff-only "$TARGET_REV"
test "$(sudo -H -u "$APP_USER" git rev-parse HEAD)" = "$TARGET_REV"
test -z "$(sudo -H -u "$APP_USER" git status --porcelain)"
sudo -H -u "$APP_USER" git log -1 --oneline
```

依赖文件未变化，不运行 pip/pnpm/apt 安装。只验证已有环境：

```bash
sudo -H -u "$APP_USER" "$APP_DIR/.venv/bin/python" --version
sudo -H -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip check
sudo -H -u "$APP_USER" test -d "$APP_DIR/frontend/node_modules"
sudo -H -u "$APP_USER" env HOME=/var/lib/paper-agent \
  /usr/local/bin/pnpm --version
node --version
```

只有 `frontend/node_modules` 意外缺失时才执行：

```bash
cd "$APP_DIR/frontend"
sudo -H -u "$APP_USER" env HOME=/var/lib/paper-agent \
  /usr/local/bin/pnpm install --frozen-lockfile
```

### 30.7 `.env`、模型、systemd 和 Nginx

本版本不改 `.env`。只核对权限，并只输出变量名、不输出值：

```bash
sudo chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
sudo chmod 600 "$APP_DIR/.env"
sudo stat -c '%U:%G %a %n' "$APP_DIR/.env"
sudo awk -F= '/^[A-Z][A-Z0-9_]*=/{print $1}' "$APP_DIR/.env" \
  | sort -u \
  | grep -E '^(APP_ENV|PUBLIC_BASE_URL|FRONTEND_ORIGIN|HF_HUB_OFFLINE|XDG_CACHE_HOME|OPENAI_API_STORAGE_ROOT)$' \
  || true
```

模型名、依赖和缓存路径未变化，本次不预热。只有缓存缺失或日志明确报离线模型不存在时，才执行：

```bash
cd "$APP_DIR"
sudo -H -u "$APP_USER" env \
  HOME=/var/lib/paper-agent \
  XDG_CACHE_HOME=/var/lib/paper-agent/cache \
  HF_HUB_OFFLINE=0 HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python - <<'PY'
from core.embeddings import get_embedder
from tools.retrieval.rerank import get_reranker
from tools.pdf.structure import get_structure_parser
assert get_embedder() is not None
assert get_reranker() is not None
parser = get_structure_parser()
if hasattr(parser, "_ensure_converter"):
    parser._ensure_converter()
print("embedding, reranker and Docling are ready")
PY
```

systemd unit 未变化，不覆盖 unit，也不执行 `daemon-reload`：

```bash
test -z "$(sudo -H -u "$APP_USER" git diff --name-only \
  "$OLD_REV..$TARGET_REV" -- deploy/systemd)"
```

Nginx 未变化，不 reload；只检查：

```bash
sudo nginx -t
sudo systemctl is-active nginx
```

### 30.8 使用真实生产 Origin 构建前端

```bash
cd "$APP_DIR/frontend"
sudo -H -u "$APP_USER" env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
```

构建失败时不要启动前端，修复后重建或直接执行 30.12 回滚。

### 30.9 启动后端、确认 schema v6，再启动其余服务

```bash
sudo systemctl start paper-agent
sleep 5
sudo systemctl --no-pager --full status paper-agent
curl -fsS --max-time 10 http://127.0.0.1:8000/health

cd "$APP_DIR"
sudo -H -u "$APP_USER" .venv/bin/python - <<'PY'
from core.api_storage_store import ApiStorageStore, SCHEMA_VERSION
from core.storage_context import StorageContext
ctx = StorageContext.openai_api()
store = ApiStorageStore(ctx)
store.initialize()
policy = store.get_policy()
assert SCHEMA_VERSION == 6
assert 1024 * 1024 <= policy.max_upload_bytes <= 200 * 1024 * 1024
print({"schema_version": SCHEMA_VERSION,
       "max_upload_mib": policy.max_upload_bytes // 1024 // 1024})
PY

sudo systemctl start paper-agent-web
sleep 3
curl -I --max-time 10 http://127.0.0.1:3000
sudo systemctl start paper-agent-cleanup.timer
sudo systemctl start paper-agent-rxiv-sync.timer
sudo systemctl is-active paper-agent
sudo systemctl is-active paper-agent-web
sudo systemctl is-active paper-agent-cleanup.timer
sudo systemctl is-active paper-agent-rxiv-sync.timer
sudo systemctl is-active nginx
```

本版本无 unit/Nginx 变化，因此不执行 `daemon-reload` 或 Nginx reload。

### 30.10 本机、公网、鉴权和文件输入冒烟

```bash
cd "$APP_DIR"
test "$(sudo -H -u "$APP_USER" git rev-parse HEAD)" = "$TARGET_REV"
test -z "$(sudo -H -u "$APP_USER" git status --porcelain)"
sudo ss -ltnp | grep -E '127\.0\.0\.1:(3000|8000)\b'
curl -fsS --max-time 10 http://127.0.0.1:8000/health
curl -fsS --max-time 10 "https://$DOMAIN/health"
curl -fsS --max-time 10 "https://$DOMAIN/api/v1/auth/config"
curl -I --max-time 10 "https://$DOMAIN/chat"

read -rsp '粘贴 pa_live_ Agent API Key: ' AGENT_KEY; echo
curl -fsS --max-time 15 "https://$DOMAIN/v1/models" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -o /tmp/paper-agent-v1-models.json
python3 - <<'PY'
import json
with open('/tmp/paper-agent-v1-models.json', encoding='utf-8') as f:
    data = json.load(f)
assert any(item.get('id') == 'paper-agent' for item in data.get('data', []))
print('authenticated /v1/models: ok')
PY
```

验证 `file_id + url` 始终使用 URL（会触发一次真实模型请求）：

```bash
curl -fsS --max-time 180 "https://$DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -o /tmp/paper-agent-file-url-smoke.json \
  --data-binary @- <<'JSON'
{
  "model": "paper-agent",
  "stream": false,
  "sessionId": "release-smoke-file-url-af3bf6d",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "只确认附件已接收，并用一句话回答。"},
      {"type": "file", "file": {
        "file_id": "qxd-release-smoke-source-id",
        "url": "https://www.rfc-editor.org/rfc/rfc20.txt",
        "filename": "release-smoke.md"
      }}
    ]
  }]
}
JSON
python3 - <<'PY'
import json
with open('/tmp/paper-agent-file-url-smoke.json', encoding='utf-8') as f:
    data = json.load(f)
assert data.get('choices'), data
print('file_id + url smoke: ok')
PY
```

验证仅有 `file_id` 时不返回 500、产生缺少 URL 的降级说明，并且不下载文件：

```bash
curl -fsS --max-time 180 "https://$DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -o /tmp/paper-agent-bare-file-id-smoke.json \
  --data-binary @- <<'JSON'
{
  "model": "paper-agent",
  "stream": false,
  "sessionId": "release-smoke-bare-file-id-af3bf6d",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "说明该附件当前是否可读取。"},
      {"type": "file", "file": {
        "file_id": "qxd-release-smoke-no-url",
        "filename": "missing-url.pdf"
      }}
    ]
  }]
}
JSON
python3 - <<'PY'
import json
with open('/tmp/paper-agent-bare-file-id-smoke.json', encoding='utf-8') as f:
    data = json.load(f)
assert data.get('choices'), data
print('bare file_id returned HTTP success with choices: ok')
PY
sudo journalctl -u paper-agent --since '3 minutes ago' --no-pager \
  | grep -F 'OpenAI-compatible file input degraded'
unset AGENT_KEY
```

上面的 RFC Editor URL 是公开、小体积的稳定 `.txt` 测试文件。若组织网络策略禁止该域名，改用已批准的
公开 HTTP(S) `.txt`/`.md`；不得使用内网、localhost、云元数据地址或含真实凭证的签名 URL。

### 30.11 清小搭、浏览器并发、日志和资源验收

浏览器人工检查：

- `/admin/api-storage` 显示“API 远程文件上限”，默认 200 MiB，可下调但不能超过 200 MiB；
- Web `/chat/upload` 仍为 20 MiB；
- 清小搭传 `file_id + file.url + filename` 时可读取文件；仅传 `file_id` 时降级而非 500；
- DOC/XLS/XLSX 能保存并提示暂不解析；PPT/PPTX 仍拒绝；
- 浏览器普通对话、历史和附件权限正常。

终端 A：

```bash
while true; do
  printf '%s auth=' "$(date '+%F %T')"
  curl -fsS -o /dev/null -w '%{http_code} %{time_total}s' \
    --max-time 2 "https://$DOMAIN/api/v1/auth/config" || printf 'FAILED'
  printf ' health='
  curl -fsS -o /dev/null -w '%{http_code} %{time_total}s' \
    --max-time 2 "https://$DOMAIN/health" || printf 'FAILED'
  echo; sleep 1
done
```

终端 B：

```bash
sudo journalctl -u paper-agent -f
```

在清小搭发起一次带文件请求，同时在浏览器 `/chat` 发普通文字请求。预期接口持续 HTTP 200，两端均能
流式完成。不得通过增加 Uvicorn worker 解决拥塞。停止监控后执行：

```bash
sudo journalctl -u paper-agent -n 300 --no-pager \
  | grep -Ei 'error|traceback|exception|schema|permission denied|out of memory|killed process' \
  || echo '后端近期日志未发现目标错误'
sudo journalctl -u paper-agent-web -n 150 --no-pager
sudo journalctl -u paper-agent-cleanup.service -n 100 --no-pager
sudo journalctl -u paper-agent-rxiv-sync.service -n 100 --no-pager
free -h
df -h / "$APP_DIR" "$BACKUP_DIR"
uptime
ps -eo pid,%cpu,%mem,rss,cmd --sort=-%mem | head -20
sudo dmesg -T | grep -Ei 'oom|out of memory|killed process' || true
sudo systemctl --failed --no-pager
```

全部通过后保留旧 revision 与备份到观察期结束，不要立即删除。

### 30.12 精确回滚：代码回到旧 commit 并恢复 v5 `state.db*`

目标版会把 `state.db` 升到 v6，而旧代码只支持 v5，因此**仅回滚 Git 不够**；必须恢复 30.5 单独
保存的升级前 `state.db*`。

```bash
set -euo pipefail
export OLD_REV='63a0799997fc7dc94fb0e140214d35b0a749b44b'
export APP_DIR='/opt/paper-agent'
export APP_USER='paper-agent'
export BACKUP_DIR='/var/backups/paper-agent/20260818-qxd-file-input-af3bf6d'
export FAILED_STATE_DIR="$BACKUP_DIR/state-db-v6-failed-$(date +%Y%m%d-%H%M%S)"

sudo test -f "$BACKUP_DIR/release-meta.txt"
sudo sh -c "cd '$BACKUP_DIR' && sha256sum -c SHA256SUMS"
sudo systemctl stop paper-agent-web
sudo systemctl stop paper-agent
sudo systemctl stop paper-agent-cleanup.timer
sudo systemctl stop paper-agent-rxiv-sync.timer
sudo systemctl stop paper-agent-cleanup.service paper-agent-rxiv-sync.service || true

sudo install -d -o root -g root -m 0700 "$FAILED_STATE_DIR"
sudo find "$APP_DIR/data/openai_api" -maxdepth 1 -type f \
  -name 'state.db*' -exec cp -a -t "$FAILED_STATE_DIR" {} +
cd "$APP_DIR"
sudo -H -u "$APP_USER" git reset --hard "$OLD_REV"
test "$(sudo -H -u "$APP_USER" git rev-parse HEAD)" = "$OLD_REV"
test -z "$(sudo -H -u "$APP_USER" git status --porcelain)"

sudo find "$APP_DIR/data/openai_api" -maxdepth 1 -type f \
  -name 'state.db*' -delete
sudo cp -a "$BACKUP_DIR/state-db-v5/." "$APP_DIR/data/openai_api/"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR/data/openai_api"

cd "$APP_DIR/frontend"
sudo -H -u "$APP_USER" env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build

sudo nginx -t
sudo systemctl start paper-agent
sleep 5
curl -fsS --max-time 10 http://127.0.0.1:8000/health
sudo systemctl start paper-agent-web
sleep 3
curl -I --max-time 10 http://127.0.0.1:3000
sudo systemctl start paper-agent-cleanup.timer
sudo systemctl start paper-agent-rxiv-sync.timer
curl -fsS --max-time 10 https://paper-agent.ycr10.cn/health
curl -fsS --max-time 10 https://paper-agent.ycr10.cn/api/v1/auth/config
curl -I --max-time 10 https://paper-agent.ycr10.cn/chat
sudo systemctl is-active paper-agent
sudo systemctl is-active paper-agent-web
sudo systemctl is-active paper-agent-cleanup.timer
sudo systemctl is-active paper-agent-rxiv-sync.timer
sudo journalctl -u paper-agent -n 200 --no-pager
free -h
df -h /
sudo dmesg -T | grep -Ei 'oom|out of memory|killed process' || true
```

若发布前独立 `state-db-v5` 目录原本为空，说明当时没有 API `state.db`，旧后端会自行创建 v5 空库；
除该明确情形外，不得跳过 v5 恢复，也不要用 v6 库启动旧代码。
