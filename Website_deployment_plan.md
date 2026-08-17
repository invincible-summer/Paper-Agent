# Paper Agent 生产部署手册（北京 ECS · 授权子域名直连 · API 网关回退）

> **当前实际状态**：使用阿里云华北 2（北京）经济型 e 4C16G；已实测北京公网出口能访问部分论文来源；已获得一个二级域名的使用授权。主路线改为“二级域名 A 记录 → 北京 ECS 公网 IPv4 → Nginx HTTPS → FastAPI/Next.js”；传统 API 网关默认二级域名仅在借用子域名不满足备案/接入条件或 DNS尚未生效时回退。
>
> **子域名使用前置条件**：父域名必须已经完成 ICP备案，且使用范围得到备案主体/域名所有者明确授权。如果父域名不是通过阿里云备案或尚未完成阿里云接入备案，不能仅靠添加 A 记录直接指向北京 ECS；应由备案主体先完成接入备案。主域名备案通常覆盖其子域名，但不会把不相干的实际服务主体自动变成合法备案主体。
>
> **你需要发给域名所有者的最小信息**：①完整二级域名名称，例如 `paper-agent.example.com`；②北京 ECS控制台显示的**公网 IPv4**；③记录类型 `A`；④TTL建议 600 秒；⑤先关闭 CDN/代理，仅做 DNS解析。不要发送 ECS私网 IP、SSH私钥、管理员密码、DeepSeek Key或 `pa_live_` Agent Key。
>
> **强制协议基线**：清小搭接入必须严格遵循 `openai-compatible-agent-integration-guide.md`，公网入口必须支持 HTTPS、Bearer鉴权、`GET /v1/models`、`POST /v1/chat/completions`、长连接 SSE、stop+usage、`[DONE]` 和可下载附件 URL。

## 0. 结论先行

### 0.1 当前固定配置

| 项目 | 当前选择 | 说明 |
|---|---:|---|
| 地域 | **华北 2（北京）** | ECS/VPC固定北京 |
| ECS | **经济型 e `ecs.e-c1m4.xlarge`，4C16G** | 1–3 人；重型 OCR/深读并发 1 |
| 系统 | **Ubuntu Server 22.04 最新点版本，x86_64** | 公共镜像 |
| 系统盘 | **100 GiB：ESSD Entry优先；控制台默认/免费额度为AutoPL时可选AutoPL** | AutoPL关闭性能突发和预配置性能；PL0也可 |
| 公网 IPv4 | **分配** | 提供给域名所有者创建 A 记录 |
| 公网计费 | **按使用流量，峰值 10 Mbps** | 论文下载和附件出站按流量计费 |
| 主公网入口 | **授权子域名 + Nginx 443** | 父域名备案/阿里云接入核验通过后启用 |
| 回退入口 | **传统 API 网关默认二级域名** | 只供测试，1000次/天且有兼容/超时限制 |
| 安全组 | **22/80/443** | 22仅管理员 `/32`；80/443全网；8000/3000不开放 |
| 登录 | **SSH密钥对** | 私钥不入库 |
| Swap | **4 GiB** | 只吸收峰值 |
| Uvicorn | **1 worker** | 不增加 worker |

### 0.2 子域名是否可直接使用的判定

| 检查 | 结果 | 下一步 |
|---|---|---|
| 父域名已有 ICP，且阿里云是接入商 | **可直接进入 DNS/HTTPS配置** | 主路线 |
| 父域名已有 ICP，但备案在其他云厂商 | **通常需要阿里云接入备案** | 完成接入备案前使用 API网关回退 |
| 父域名未备案 | **不能指向北京 ECS正式公网服务** | 先备案或回退 API网关测试域名 |
| 域名所有者未明确授权 | **不能使用** | 获得书面/可追溯授权 |
| 域名 CAA限制目标 CA | 证书可能签发失败 | 让域名所有者调整 CAA或提供证书 |

### 0.3 论文来源据实启用

北京 ECS能出公网不代表所有境外论文站点稳定。本手册不再假定全源可达：部署后对 OpenAlex、Crossref、arXiv、Semantic Scholar、Europe PMC、DOAJ、HAL、OpenAIRE 和真实 OA PDF分别测试；只启用实测稳定来源。`401/403/429` 表示网络可达但需凭证/限流处理，`000`、DNS失败和持续超时才判定不可用。全文仍严格 OA-only，不通过校园账号、代理或订阅库自动抓取。

---

## 1. 主路线与回退路线

### 主路线：授权子域名直连北京 ECS

```text
清小搭/浏览器
  → https://paper-agent.example.com:443
  → DNS A记录
  → 北京 ECS公网 IPv4
  → Nginx
  ├── /v1/*       → FastAPI 127.0.0.1:8000
  ├── /api/v1/*   → FastAPI 127.0.0.1:8000
  ├── /files/*    → FastAPI 127.0.0.1:8000
  ├── /elements/* → FastAPI 127.0.0.1:8000
  └── /            → Next.js 127.0.0.1:3000
```

主路线优势：

- 没有 API网关默认域名1000次/天限制；
- 没有默认 `Content-Disposition` 兼容风险；
- 没有 Serverless 60秒后端超时；
- Nginx可将 SSE和深读超时设为3600秒；
- 自有前端、附件和清小搭共用同一个 HTTPS Origin。

### 回退路线：传统 API 网关默认域名

```text
清小搭
  → https://xxxxxxxx-cn-beijing.alicloudapi.com/v1
  → API网关 VPC授权
  → ECS私网IP:8080
  → Nginx
  → FastAPI 127.0.0.1:8000
```

仅在以下情况启用：

- 域名所有者尚未添加 A记录；
- DNS尚未生效；
- 阿里云接入备案尚未完成；
- HTTPS证书暂未签发；
- 只需三天短对话/探测。

云助手端口转发不属于公网服务入口，不能用于清小搭。

---

## 2. 在阿里云控制台购买 ECS（第一次部署逐按钮版）

> 本节按阿里云 ECS 官方“自定义购买”流程整理，控制台按钮名称核对日期为 **2026-08-16**。阿里云会灰度更新界面，若文字略有变化，优先按本节给出的“配置目标”核对，不要只凭按钮位置操作。正式下单前，务必在右侧费用明细中重新确认地域、计费方式、CPU/内存、系统盘、公网带宽和购买时长。

### 2.0 下单前先决定四件事

#### 2.0.1 地域：固定选择华北 2（北京）

本次所有地域级资源都选择：

```text
地域：华北 2（北京）
```

包括 ECS、VPC、vSwitch、普通安全组、API 网关实例/API 分组和 VPC 授权。可用区只需选择仍有 `ecs.e-c1m4.xlarge` 库存的北京可用区；vSwitch 必须属于同一可用区。

#### 2.0.2 计费：前三天按量，备案后再转长期

本次三天部署选择：

```text
ECS：按量付费
API 网关：短对话用 Serverless；完整深读用按量专享实例
自动释放：不设置
释放保护：开启
费用预警：当天创建
```

不要选择抢占式实例。三天结束后如果继续运行，先完成容量和清小搭验证，再决定转包年包月；办理 ICP 时再确保实例满足备案控制台要求。

#### 2.0.3 实例规格族：本次固定选择经济型 e 4C16G

本次不再在 U/c/g 之间选择，购买页目标固定为：

```text
规格族：经济型实例规格族 e
实例规格：ecs.e-c1m4.xlarge
vCPU：4
内存：16 GiB
架构：x86_64
```

阿里云官方规格表中，`ecs.e-c1m4.xlarge` 是 4 vCPU / 16 GiB。经济型 e 使用非绑定 CPU 调度，适合轻载智能体网关、中小网站和开发测试；本项目可以运行，但必须接受 Docling/OCR 期间的 CPU 性能波动。

购买页如果看不到该型号：

1. 先确认筛选条件是“4 vCPU / 16 GiB”，规格族是“经济型 e”；
2. 换同一地域的其他可用区；
3. 仍无库存时，不要降到 2C4G，也不要改成 ARM；
4. 可以选择同为 4C16G 的通用型 `g8i/g9i`，或暂停购买等待库存；
5. 不选择突发性能 t、抢占式、GPU、本地盘或共享型老规格。

容量边界补充：**2 vCPU / 4 GiB** 只适合 API-only 的受限轻量档（不运行 Next.js，且同一时刻最多一个重任务），不是本手册的生产目标；当前 1–3 人混合使用仍按 **4 vCPU / 16 GiB**、单 Uvicorn worker 部署。容量不足时升级实例，不要长期依赖 Swap，也不要通过增加 Uvicorn worker 绕过进程内状态边界。

经济型 e 与其他规格的关系只作为后续升级参考：

| 规格 | 什么时候换 |
|---|---|
| 通用算力型 U 4C8G | 内存实际很低，但希望 CPU 比 e 更稳定、价格仍较低 |
| 计算型 c 4C8G | OCR/Docling CPU 时延比内存余量更重要 |
| 通用型 g 4C16G | 保持 16 GiB，同时要求更稳定的企业级 CPU 性能；本项目首选升级目标 |
| 8C32G | 经常多份扫描 PDF 或多路重任务重叠 |

#### 2.0.4 域名：先决定最终访问地址

生产环境推荐准备：

```text
paper-agent.example.com
```

或 API-only：

```text
api.example.com
```

必须使用 HTTPS，不能长期使用 `http://公网IP:8000`。如果还没有域名，可以先通过 SSH 和本机 `curl http://127.0.0.1:8000/health` 完成后端安装，但不要把 Bearer Key 放在公网 HTTP 中测试。

### 2.0.5 北京 ECS 订单参数总表

| 控制台参数 | 本次选择 | 备注 |
|---|---|---|
| 产品 | 云服务器 ECS | 不是轻量应用服务器 |
| 购买方式 | 自定义购买 | — |
| 地域 | 华北 2（北京） | 后续 API 网关也必须北京 |
| 付费类型 | 按量付费 | 三天临时使用 |
| 实例规格族 | 经济型实例规格族 e | — |
| 实例规格 | `ecs.e-c1m4.xlarge` | 4 vCPU / 16 GiB |
| 数量 | 1 台 | — |
| 镜像 | Ubuntu 22.04 64 位最新公共镜像 | x86_64 |
| 系统盘 | 100 GiB，优先ESSD Entry；当前订单提供AutoPL免费额度时可用AutoPL | AutoPL不启用性能突发/预配置性能 |
| 数据盘 | 不添加 | — |
| 文件备份 | 不开通/取消勾选“开通服务，激活备份” | 每日全文件备份保留30天，会绕过上传/Checkpoint等应用TTL |
| 自动快照 | 不选择系统盘自动快照策略 | 整盘快照同样会延长临时数据保留期 |
| VPC | `paper-agent-vpc` 或北京默认 VPC | 记录 VPC ID |
| vSwitch | 有目标库存的北京可用区 | 记录 vSwitch ID |
| 私网 IPv4 | 自动分配 | API 网关 VPC 授权使用 |
| 公网 IPv4 | 分配 | 发给域名所有者作为 A 记录值 |
| 公网计费 | 按使用流量，峰值 10 Mbps | 不买 NAT 网关 |
| IPv6 | 不启用 | — |
| 安全组 | 普通安全组 `paper-agent-sg` | 主路线开放22/80/443；回退网关才增加8080来源 |
| 22/TCP | 管理员公网 IP `/32` | 不允许全网 |
| 80/443 | 父域名备案/接入核验通过后开放全网 | HTTPS主入口 |
| 8080/TCP | 主路线不开放 | 只有启用回退 API网关时才放行网关出口 IP |
| 8000/3000 | 永不公网开放 | 127.0.0.1监听 |
| 登录 | SSH 密钥 `paper-agent-admin` | — |
| 实例名称 | `paper-agent-prod-01` | — |
| 标签 | `app=paper-agent`、`env=prod` | — |
| 云助手 | 保持默认启用 | 只用于运维，不作为清小搭入口 |
| User Data | 留空 | — |
| 自动释放 | 不设置 | — |
| 释放保护 | 开启 | — |

---

### 2.1 进入购买页

1. 登录阿里云控制台，先完成实名认证，并为阿里云账号启用 MFA；
2. 在控制台顶部搜索框输入 **“云服务器 ECS”**，点击进入“云服务器 ECS”控制台；
3. 左侧导航进入 **“实例与镜像” → “实例”**；
4. 先看页面左上角的地域下拉框，切换到你准备部署的地域；
5. 点击右上角或列表上方的 **“创建实例”**；
6. 如果出现“快速购买”和“自定义购买”，选择 **“自定义购买”**。快速购买隐藏了部分网络、安全组和磁盘选项，不适合第一次正式部署。

不要在尚未确认地域时直接下单。ECS 实例、VPC、安全组和云盘都是地域级资源，页面切错地域会让后续找不到刚创建的资源。

---

### 2.2 “基础配置”逐项选择

#### 步骤 1：付费类型

```text
付费类型：按量付费
抢占式：不选
自动释放：不设置
释放保护：创建后立即开启
```

阿里云按量 ECS 创建时会实时校验账户余额。三天临时阶段不要为了备案直接购买长期实例；先把清小搭协议和 API 网关兼容性验证完。

#### 步骤 2：地域和可用区

```text
地域：华北 2（北京）
可用区：选择有 ecs.e-c1m4.xlarge 库存的任一北京可用区
```

vSwitch 必须属于同一可用区。API 网关只要求同地域/VPC，不要求和 ECS 同一可用区。

#### 步骤 3：实例规格

在“实例规格”区域按下面顺序操作：

1. 架构选 `x86`；
2. 规格族选择 **“经济型实例规格族 e”**；
3. vCPU 筛选 `4`；
4. 内存筛选 `16 GiB`；
5. 在结果中选中：

```text
ecs.e-c1m4.xlarge
4 vCPU / 16 GiB
```

选中后再次查看右侧配置摘要，确认不是 `ecs.e-c1m2.xlarge`（4C8G），也不是突发性能 t。购买页若没有精确型号，返回 §2.0.3 按库存处理。

#### 步骤 4：镜像

依次选择：

1. **公共镜像**；
2. **Ubuntu**；
3. **Ubuntu 22.04 64 位的最新点版本**；
4. 架构确认是 **x86_64/AMD64**。

不要选择：

- Windows Server；
- 带预装面板的云市场镜像；
- ARM64 镜像；
- 来历不明的第三方镜像；
- 已经带 Nginx/MySQL/宝塔等环境的一键镜像。

本手册全部命令按干净的 Ubuntu Server 22.04 公共镜像编写。为减少安全更新和内核兼容问题，选择购买页提供的最新 Ubuntu 22.04 点版本；如果目标规格与镜像无法组合，先换同地域其他可用区或兼容的 x86 实例，不要临场改 ARM 或第三方镜像。

#### 步骤 5：存储

本次经济型 e 的系统盘容量固定为 **100 GiB**，云盘类型按购买页实际可选项和费用选择：

```text
首选低成本：ESSD Entry 100 GiB
当前页面默认且100 GiB在免费额度内：ESSD AutoPL 100 GiB
其他可用回退：ESSD PL0 100 GiB
数据盘：不添加
```

经济型 e 当前支持 ESSD AutoPL。AutoPL 的基础性能相当于 ESSD PL1，但“性能突发”和“预配置性能”属于可选计费能力；Paper Agent 的SQLite、Chroma、模型缓存和单路PDF/OCR不需要额外购买这两项，所以保持关闭。实例本身也有云盘IOPS/吞吐上限，开启额外性能不一定能实际发挥。

其他说明：

- 60 GiB 只是轻量最低值；本项目 Python 环境、Hugging Face/Docling 模型、PDF、元素裁图、Chroma、SQLite、导出和日志会持续增长；
- 第一次部署不需要额外数据盘，单系统盘最容易维护；
- 不选择本地盘实例；
- 云盘后续可以扩容，但扩容后仍需在操作系统内扩分区/文件系统；云盘不能缩容；
- 理解释放实例时系统盘数据会丢失，按量实例必须开启释放保护。

**文件备份/自动快照与本项目 TTL 的冲突：** 购买页的“文件备份基础版”会每天自动备份文件并保留约30天；整盘自动快照也会把运行数据保留到快照删除为止。两者都会让 API私有上传、Checkpoint、导出、对话或PDF超过应用内24小时/7天等TTL。因此本次取消勾选“开通服务，激活备份”，系统盘自动快照策略保持空白。需要灾备时按 §13 只备份必要的 `users.db`、配置和管理员策略，并对含密钥备份加密。

---

### 2.3 “网络和安全组”逐项选择

#### 步骤 1：专有网络 VPC

单机首次部署不需要购买 NAT 网关、负载均衡或弹性网卡。选择：

```text
专有网络：默认 VPC，或新建 paper-agent-vpc
交换机：  当前可用区的默认 vSwitch，或新建 paper-agent-vswitch
IPv6：    暂不启用
```

如果需要新建：

1. 在 VPC 下拉框旁点击 **“创建专有网络”**；
2. 名称填写 `paper-agent-vpc`；
3. IPv4 网段可使用默认推荐值，例如 `192.168.0.0/16`；
4. 在当前可用区创建 vSwitch，例如 `192.168.1.0/24`；
5. 返回 ECS 购买页刷新并选中它。

只有准备通过 VPN/专线接入现有公司网络时，才需要提前设计避免冲突的私网网段。

#### 步骤 2：公网 IP、带宽、域名和服务器出网

```text
分配公网 IPv4：是
公网线路：默认 BGP
带宽计费：按使用流量
峰值：10 Mbps
IPv6：不启用
```

创建后从 ECS控制台实例详情复制“公网 IPv4”，不要用私网地址、`hostname -I` 的VPC地址或 API网关出口 IP。将公网 IPv4和准备使用的完整二级域名发给域名所有者创建 A记录。

公网 IPv4同时用于：

1. 域名 HTTPS入站；
2. 管理员 SSH；
3. ECS访问论文 API、OA PDF、DeepSeek/VLM和系统软件源。

完成 §3 后按 §0.3/§9 的脚本实测论文来源，不根据“有公网 IP”推断所有境外来源必然可达。

#### 步骤 3：安全组

创建普通安全组：

```text
名称：paper-agent-sg
```

确认父域名备案/阿里云接入条件满足后，主路线使用：

| 协议 | 端口 | 来源 | 用途 |
|---|---:|---|---|
| TCP | 22 | 管理员公网 IP `/32` | SSH |
| TCP | 80 | `0.0.0.0/0` | ACME证书验证和 HTTP跳HTTPS |
| TCP | 443 | `0.0.0.0/0` | 清小搭与浏览器 HTTPS |

永远不开放：

```text
8000
3000
数据库端口
全部端口
```

主路线不需要8080。只有回退到 §9.10 的 API网关方案时，才添加 TCP 8080，来源必须是 API网关控制台显示的全部出口 IP `/32`，不能全网开放。

---

### 2.4 “管理设置”逐项选择

#### 步骤 1：登录凭证优先选 SSH 密钥对

推荐选择 **密钥对**，不要把 root 密码作为长期登录方式。

如果还没有密钥对：

1. 在“登录凭证/密钥对”位置点击 **“创建密钥对”**，或打开 ECS 控制台 → **“网络与安全” → “密钥对”**；
2. 点击 **“创建密钥对”**；
3. 名称填写 `paper-agent-admin`；
4. 选择由阿里云自动创建密钥对；
5. 点击确认后，浏览器会下载 `.pem` 私钥文件；
6. 立即将它移动到本机受保护目录，并在密码管理器记录用途；私钥通常只提供这一次下载机会；
7. 返回购买页刷新，选择 `paper-agent-admin`。

Linux/macOS/WSL 上先执行：

```bash
chmod 600 ~/.ssh/paper-agent-admin.pem
```

禁止：

- 把 `.pem` 放进本仓库；
- 通过聊天软件、邮件或公开网盘传私钥；
- 把私钥内容粘贴进 `.env`；
- 在截图、日志、Issue 中展示私钥。

如果页面选择了“安全加固”模式，Ubuntu 登录用户名通常可能显示为 `ecs-user`；非加固镜像也可能使用 `root`。**以实例“连接”页面显示的默认用户名为准，不要反复猜密码。** 应用进程后面始终使用独立的 `paper-agent` 低权限账号。

#### 步骤 2：名称和标签

建议填写：

```text
实例名称：paper-agent-prod-01
主机名：  paper-agent-prod-01
资源组：  默认资源组（个人账号足够）
标签：    app=paper-agent, env=prod
```

标签不是安全控制，但以后查账单、监控和避免误删时很有用。

#### 步骤 3：高级选项

- 实例元数据访问模式保持较安全的默认值；
- 不填写自定义 User Data。第一次部署应逐步执行本手册，避免一段脚本失败后难以定位；
- 云助手可以保留默认启用；
- 按量实例勾选“释放保护”；
- 不设置自动释放时间；
- 不启用自动续费前，先确认阿里云账号的到期提醒方式；长期包年包月实例建议开启自动续费。

---

### 2.5 下单前最后一页逐项核对

点击 **“下一步/确认订单”** 后，不要立即付款。对照右侧配置清单逐行确认：

```text
地域：      华北 2（北京）
计费：      按量或已选正式计费，非抢占式
实例：      ecs.e-c1m4.xlarge，4C16G，x86_64
镜像：      Ubuntu 22.04 最新公共镜像
系统盘：    100 GiB；ESSD Entry优先，当前免费额度可选AutoPL（两项增强关闭）
公网 IPv4：已分配，稍后发给域名所有者
公网计费：  按使用流量，峰值 10 Mbps
主域名状态：父域名 ICP和阿里云接入已核验
二级域名：  已获得明确使用授权
安全组：    22仅管理员/32；80/443全网；8000/3000关闭
登录：      paper-agent-admin SSH密钥
数量：      1台
释放保护：  开启
```

同时检查费用明细中是否误加入：

- 数据盘；
- GPU；
- 负载均衡；
- NAT 网关；
- 云数据库；
- 云市场付费镜像；
- 多台实例；
- 不需要的长期快照套餐。

确认服务协议和金额后，点击 **“创建实例/立即购买/确认下单”**。按钮名称可能因包年包月或按量模式不同。等待实例状态变为 **“运行中”**。

---

### 2.6 创建后第一次检查

1. 返回 ECS 控制台 → **“实例与镜像” → “实例”**；
2. 左上角切到购买时的地域；
3. 找到 `paper-agent-prod-01`，确认状态是“运行中”；
4. 记录公网 IPv4、私网 IPv4、VPC ID 和 vSwitch ID；
5. 点击实例 ID进入详情页；
6. 在“安全组”页签确认当前只有管理员 `/32` 的 22 规则，没有全网 80/443/8080/8000/3000；
7. 在“云盘”页签确认系统盘约 100 GiB；
8. 在“监控”页签确认已有 CPU、网络和磁盘基础图表；
9. 确认实例详情显示公网 IPv4，且公网带宽为“按使用流量、峰值 10 Mbps”；该 IP只用于运维和出网；
10. 用 §2.3 的命令验证 OpenAlex/arXiv/Crossref 等外部站点可达；
11. 按量实例确认释放保护已开启；包年包月实例确认到期时间和续费提醒。

如果实例列表为空，第一件事是检查控制台左上角地域是否选错，不要立刻重复购买。

---

### 2.7 第一次登录服务器

#### 方法 A：用本机 SSH（长期推荐）

在实例详情点击 **“连接”**，查看阿里云提示的默认用户名。然后在自己的电脑执行：

```bash
ssh -i ~/.ssh/paper-agent-admin.pem ecs-user@你的公网IP
```

如果实例页面明确显示用户名是 `root`，则使用：

```bash
ssh -i ~/.ssh/paper-agent-admin.pem root@你的公网IP
```

第一次连接会显示主机指纹。先在实例详情确认公网 IP 没抄错，再输入 `yes`。Windows 10/11 可以直接在 PowerShell 使用系统自带的 `ssh`；也可以使用 WSL。

#### 方法 B：阿里云 Workbench（SSH 故障排查）

1. ECS 实例列表找到服务器；
2. 点击右侧 **“连接”**；
3. 在 Workbench 方式中点击 **“立即登录”**；
4. 选择密钥、免密或页面提供的连接方式；
5. 优先选择“免密连接/会话管理”；如果改用 Workbench 的 SSH 终端连接，安全组可能还需按阿里云官方 Workbench 文档临时放行其服务网段，不能为此改成 `0.0.0.0/0:22`；
6. 登录后只用于检查和修复 SSH，不要长期依赖共享浏览器会话。

如果 SSH 失败，按顺序检查：

| 现象 | 先检查 |
|---|---|
| `Connection timed out` | 实例是否运行、公网 IP 是否正确、安全组 22 是否允许你当前公网 IP |
| `Permission denied (publickey)` | 用户名是否与实例页面一致、`.pem` 是否对应当前地域实例、权限是否为 600 |
| 昨天能连今天不能 | 家庭/公司公网 IP 是否变化，更新安全组 `/32` |
| 控制台也找不到实例 | 是否切错地域，是否误释放，账号是否正确 |

---

### 2.8 登录后的系统初始化

先只做检查，不要马上执行来源不明的一键安装脚本：

```bash
whoami
cat /etc/os-release
uname -m
nproc
free -h
df -hT
ip -br address
```

期望看到：

```text
Ubuntu 22.04
x86_64
4 个 CPU
约 8 GiB 或 16 GiB 内存
根文件系统约 100 GiB
```

更新基础系统：

```bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y
sudo timedatectl set-timezone Asia/Shanghai
sudo apt install -y unattended-upgrades
sudo systemctl enable --now unattended-upgrades
sudo reboot
```

重启约 1–3 分钟后重新 SSH 登录，再检查：

```bash
uptime
free -h
df -h
```

然后继续执行 §3 安装依赖、创建 Swap 和应用账号。

#### 可选：确认密钥登录成功后关闭密码登录

只有在**新开第二个终端也能使用密钥登录成功**后，才执行：

```bash
sudo tee /etc/ssh/sshd_config.d/99-paper-agent.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sudo sshd -t
sudo systemctl reload ssh
```

不要在唯一一个 SSH 会话里未经测试就关闭登录方式，否则可能把自己锁在服务器外。

#### 可选：启用 UFW 作为第二层防火墙

确认 SSH密钥登录正常、域名备案/接入核验通过后，主路线配置：

```bash
ADMIN_IP='把这里替换成你的公网IPv4'
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from "${ADMIN_IP}" to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

启用前先确认阿里云控制台会话管理/VNC恢复方式可用。主路线不开放8080；只有启用API网关回退时，才为每个网关出口 IP增加8080规则。

---

### 2.9 将公网 IP和二级域名信息发给域名所有者

#### 2.9.1 先让域名所有者确认三件事

在添加 A记录前，要求域名所有者确认：

1. 父域名已经完成 ICP备案；
2. 当前备案接入商是否包含阿里云；如果不是，应由备案主体办理阿里云接入备案；
3. 备案主体明确允许该子域名用于 Paper Agent/清小搭服务，并愿意配合 DNS和证书验证。

主域名备案通常覆盖子域名，但借用子域名不等于借到备案主体身份。实际服务内容、主体和备案范围不匹配时仍有风险；无法确认就使用 API网关回退，不要直接解析。

#### 2.9.2 从 ECS控制台复制正确 IP

路径：

```text
ECS控制台 → 实例与镜像 → 实例
→ 华北2（北京）
→ paper-agent-prod-01
→ 公网IPv4
```

复制形如：

```text
123.123.123.123
```

不要发送：

```text
192.168.x.x / 172.16.x.x / 10.x.x.x 私网IP
API网关出口IP
SSH私钥
管理员密码
DeepSeek/VLM Key
pa_live_ Agent API Key
```

#### 2.9.3 发给域名所有者的消息模板

将以下模板中的值替换后，通过私密渠道发送：

```text
请为 Paper Agent 添加一条 DNS解析：

完整二级域名：paper-agent.example.com
记录类型：A
主机记录：paper-agent
记录值：123.123.123.123
解析线路：默认
TTL：600秒（或控制台默认）
代理/CDN：先关闭，仅DNS解析

用途：北京 ECS 的 HTTPS API 和 Web 前端。
请同时确认父域名已完成ICP备案且已接入阿里云；如存在CAA限制，请告知允许的证书CA。
```

如果实际分配的是：

```text
research.agent.example.com
```

主机记录通常填写：

```text
research.agent
```

最终以域名 DNS服务商控制台为准。

#### 2.9.4 域名所有者在 DNS控制台操作

如果域名使用阿里云云解析 DNS：

```text
云解析 DNS
→ 权威域名解析
→ 找到父域名
→ 解析设置
→ 添加记录
```

选择：

```text
记录类型：A
主机记录：约定的子域名前缀
记录值：北京 ECS 公网IPv4
解析线路：默认
TTL：600秒或默认值
```

如果域名托管在 Cloudflare 等服务商，初次必须选择“仅 DNS/DNS only”，不要开启代理/CDN；代理可能缓存或中断 SSE，等直连验收完成后再单独评估。

#### 2.9.5 在本地和 ECS验证 DNS

```bash
DOMAIN='paper-agent.example.com'
EXPECTED_IP='123.123.123.123'

getent ahostsv4 "$DOMAIN"
dig +short A "$DOMAIN"
```

结果必须包含 `EXPECTED_IP`。没有 `dig` 时安装：

```bash
sudo apt install -y dnsutils
```

还要检查父域名 CAA：

```bash
dig +short CAA example.com
dig +short CAA paper-agent.example.com
```

无 CAA记录通常表示不额外限制 CA；如果存在 CAA且不允许计划使用的 CA，证书签发会失败，需要域名所有者调整或直接提供有效证书。

#### 2.9.6 证书验证需要域名所有者继续配合的情况

可选方式：

1. **HTTP验证**：A记录已生效、80端口开放后，由 Certbot自动验证；最省事；
2. **DNS验证**：证书平台给出 TXT记录，由域名所有者添加；适合80暂时不可达；
3. **域名所有者提供证书**：可提供覆盖该子域名的单域名或通配符证书，私钥必须通过安全渠道传输并限制权限。

不要要求域名所有者把整个 DNS账号密码交给你；让对方添加指定 A/TXT记录即可。

#### 2.9.7 上线后备案信息和公安联网备案

北京 ECS正式对外服务后：

- 网站页面应按备案要求展示 ICP备案号并链接工信部备案系统；
- 服务实际内容必须与备案主体/网站信息一致；
- 由备案主体在开通后30日内办理公安联网备案；
- 借用子域名的情况下，域名所有者/备案主体必须参与这些后续事项，不能只添加一条 A记录后不再配合。

如果对方无法承担备案主体和后续管理责任，应停止使用该子域名并回退 API网关测试入口或更换为你自己可备案的域名。

---

### 2.10 创建当天必须完成的费用与安全设置

- 费用与成本控制台：设置余额、按量费用和月度预算提醒；
- ECS：按量实例开启释放保护，包年包月实例设置到期提醒或自动续费；
- 云监控：至少为 CPU 持续 >80%、磁盘 >80%、实例不可用设置告警；内存/磁盘使用率指标需要时安装或启用云监控插件；
- 安全组：主路线确认22仅管理员 `/32`，80/443全网，8000/3000/数据库端口未开放；8080只在启用API网关回退时按出口IP `/32` 添加；
- RAM：日常运维尽量使用 RAM 子账号，不长期共享主账号；主账号启用 MFA；
- 私钥：确认只有管理员本人可读，并制作一份加密离线备份；
- 账单：下单后的第一天和第一周各检查一次账单，确认没有误购 NAT 网关、EIP、负载均衡、快照或多余磁盘。

完成本节后，才进入 §3 的 Linux 环境安装。

---

## 3. 安装系统运行环境

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential nginx \
  software-properties-common poppler-utils tesseract-ocr \
  certbot python3-certbot-nginx dnsutils \
  libgl1 libglib2.0-0

# Ubuntu 22.04 默认只有 Python 3.10；项目要求 Python >= 3.11。
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
python3.11 --version              # 必须是 Python 3.11.x
```

> `ppa:deadsnakes/ppa` 是 Ubuntu 22.04 上安装并行 Python 3.11 的明确步骤；不要替换或删除系统自带 Python 3.10。若组织策略禁止 PPA，改用经过校验的 Miniforge Python 3.11，并把下文所有 `python3.11` 保持指向该解释器。

安装 **Node.js 22 LTS** 后启用 Corepack/pnpm。本手册固定 Node 22 LTS，仓库已在该版本验证；Next.js 14 的最低要求为 Node 18.17。Ubuntu 22.04 自带仓库的 Node 版本可能过旧，因此先使用 NodeSource 的 22.x 安装脚本配置 APT 源，再安装 `nodejs`：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh
# 首次部署建议先审阅脚本；不要直接执行来源不明的 curl | bash。
less /tmp/nodesource_setup.sh
sudo -E bash /tmp/nodesource_setup.sh
sudo apt install -y nodejs
node --version                    # 应为 v22.x
npm --version
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
sudo -H -u paper-agent .venv/bin/pip install --upgrade pip

# 经济型 e 是 CPU-only 主机。必须先从 PyTorch 官方 CPU 索引安装，
# 否则 Linux 默认 PyPI 轮子可能拉取数 GiB 的 CUDA/NVIDIA 依赖。
sudo -H -u paper-agent .venv/bin/pip install -r requirements-cpu.txt

# requirements.txt 会加载 constraints.txt，固定仓库已验证的版本组合，
# 避免 LangChain/LangSmith 在首次安装时大范围依赖回溯。
sudo -H -u paper-agent .venv/bin/pip install -r requirements.txt -e backend
sudo -H -u paper-agent env HOME=/var/lib/paper-agent bash -c 'cd /opt/paper-agent/frontend && /usr/local/bin/pnpm install --frozen-lockfile'
```

安装后必须确认使用 CPU 轮子且依赖一致：

```bash
sudo -H -u paper-agent .venv/bin/python - <<'PY'
import torch
print(torch.__version__)
print(torch.version.cuda)
assert "+cpu" in torch.__version__, torch.__version__
assert torch.version.cuda is None, torch.version.cuda
PY
sudo -H -u paper-agent .venv/bin/python -m pip check
```

预期 `torch.__version__` 含 `+cpu`、`torch.version.cuda` 为 `None`，且 `pip check` 输出 `No broken requirements found.`。后端上传路由使用 FastAPI `File` / `UploadFile`，因此 `backend/pyproject.toml` 明确依赖 `python-multipart`，并由 `constraints.txt` 固定版本；服务日志若出现 `Form data requires "python-multipart"`，说明服务器代码或依赖尚未同步完整，不应把该包当作手工维护的服务器特例。

如果 pip 长时间连续枚举数十个 `langsmith` / `langchain-openai` 版本，立即 `Ctrl+C`；这表示没有使用当前仓库的 `constraints.txt`，不要继续等待。

如果安装曾在解析阶段中断，下载的是可安全删除的 pip 缓存，不是已安装的多份包。可在重新安装前执行：

```bash
sudo -H -u paper-agent .venv/bin/pip cache info
sudo -H -u paper-agent .venv/bin/pip cache purge
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
PUBLIC_BASE_URL=https://paper-agent.ycr10.cn

DEEPSEEK_API_KEY=<填写 DeepSeek 官方 API Key>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_LIGHT=deepseek-v4-flash
DEEPSEEK_MODEL_REASONING=deepseek-v4-flash

# 自有前端生产策略：要求账号登录、关闭公开注册、禁止游客。
AUTH_REQUIRED=true
REGISTRATION_OPEN=false
GUEST_ACCESS=false
FRONTEND_ORIGIN=https://paper-agent.ycr10.cn
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

- 当前主路线将 `PUBLIC_BASE_URL` 和 `FRONTEND_ORIGIN` 都设为 `https://paper-agent.ycr10.cn`，不带 `/v1`、端口或末尾路径。若回退 API网关，则仅将 `PUBLIC_BASE_URL` 临时改为网关默认 Origin；临时网关不暴露自有前端。
- DeepSeek 官方当前使用 `deepseek-v4-flash` / `deepseek-v4-pro`；本项目 1–3 人部署先统一使用 `deepseek-v4-flash`，工具调用与思考开关由代码按调用类型控制。旧别名 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 退役，不要再写入新部署。
- `DEEPSEEK_*`/`MULTIMODAL_*` 是模型服务商密钥；**Agent API Key 不是模型密钥**。
- `AGENT_API_KEY` 仍可作为迁移/应急凭证，但正式部署推荐由管理员生成数据库密钥；生产环境没有任何可用 Agent Key 时 `/v1` 返回 503，错误或已撤销密钥返回 401。
- `.env`、`*.key`、`*.pem` 不得提交、截图、复制到工单或日志中；仓库只保留无真实值的 `.env.example`。

---

## 6. 初始化管理员并创建给清小搭的 Agent API Key

清小搭截图中的“API 密钥”不是 DeepSeek Key、不是服务器 SSH 私钥、不是管理员密码，而是本项目签发的长期 **Agent API Key**。它以 `pa_live_` 开头，只用于：

```http
Authorization: Bearer pa_live_...
```

调用本项目的：

```text
GET  /v1/models
POST /v1/chat/completions
```

### 6.1 第一次创建管理员

管理员固定标识：

```text
用户名：administrator
邮箱：administrator@administrator
角色：administrator
```

在服务器执行：

```bash
cd /opt/paper-agent
sudo -u paper-agent .venv/bin/python scripts/bootstrap_administrator.py
```

终端会提示两次输入密码，输入时不会显示字符。密码至少 8 位，建议使用密码管理器生成 16 位以上随机密码。不要把密码写进命令行、`.env`、部署手册、截图或 shell 脚本。

预期输出：

```text
管理员 administrator 已创建。
```

脚本幂等：如果管理员已经存在，只会提示已存在，不会重置密码，也不会把同名普通用户自动提升为管理员。

确认数据库已经在全新的生产运行目录创建：

```bash
sudo -u paper-agent test -f /opt/paper-agent/data/users.db && echo 'users.db ready'
sudo chmod 600 /opt/paper-agent/data/users.db
sudo chown paper-agent:paper-agent /opt/paper-agent/data/users.db
sudo stat -c '%U:%G %a %n' /opt/paper-agent/data/users.db
```

不得从开发机复制旧 `users.db`，也不得提交到 Git。

### 6.2 创建清小搭生产 Agent API Key

运行交互式脚本：

```bash
cd /opt/paper-agent
sudo -u paper-agent .venv/bin/python scripts/create_agent_api_key.py \
  --username administrator \
  --name '清小搭生产接入'
```

输入刚才的管理员密码后，脚本会输出：

```text
Agent API Key 已创建。完整密钥只显示这一次，请立即安全保存：
pa_live_这里是一长串随机字符
```

立即完成以下操作：

1. 将完整 `pa_live_...` 保存到密码管理器，名称写“Paper Agent / 清小搭生产接入”；
2. 不要关闭终端后再寻找明文——数据库只保存 SHA-256 哈希，完整值无法恢复；
3. 不要把它写入 `.env`；正式数据库 Key 不需要配置 `AGENT_API_KEY=`；
4. 不要把它发进聊天、Issue、日志、截图或测试文件；
5. 如果泄露，从管理员页面撤销旧 Key，再创建一个新的，不要继续使用。

`AGENT_API_KEY` 环境变量只保留给迁移/应急场景，正式清小搭接入使用上述数据库签发 Key。

### 6.3 启动后在服务器本机验证 Key

完成 §7 后端 systemd 启动后，在服务器执行。`read -s` 可以避免 Key 进入 shell 历史：

```bash
read -rsp '粘贴 pa_live_ Agent API Key: ' KEY; echo

curl -i http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $KEY"

curl -sS -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"只回复：连接成功"}]}'

unset KEY
```

预期：

- `/v1/models` 返回 HTTP 200；
- 非流式对话返回 JSON，包含 `choices[0].message.content`；
- 错误 Key 返回 401；
- 如果返回 503，表示生产环境没有可用数据库 Key，重新检查 Key 是否创建在 `/opt/paper-agent/data/users.db`。

### 6.4 从授权子域名公网 HTTPS验证 Key

完成 §9 的 DNS、证书和 Nginx后，在自己的电脑执行。Windows PowerShell 不支持 Bash 的 `read -rsp`，按下面方式隐藏输入并且只把变量名写入命令历史：

```powershell
$SecureKey = Read-Host '粘贴 pa_live_ Agent API Key' -AsSecureString
$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
$KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($BSTR)
$BASE = 'https://paper-agent.ycr10.cn/v1'

curl.exe -i "$BASE/models" -H "Authorization: Bearer $KEY"

# Windows PowerShell 5.1 容易在把内联 JSON 传给原生 curl.exe 时破坏引号；
# 使用无 BOM 的 UTF-8 临时文件最稳妥，也避免长命令在引号中间被终端换行。
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$JsonFile = Join-Path $env:TEMP 'paper-agent-request.json'

$Body = @{
  messages = @(@{ role = 'user'; content = '只回复：非流式成功' })
} | ConvertTo-Json -Depth 5 -Compress
[System.IO.File]::WriteAllText($JsonFile, $Body, $Utf8NoBom)

curl.exe -sS -X POST "$BASE/chat/completions" `
  -H "Authorization: Bearer $KEY" `
  -H "Content-Type: application/json" `
  --data-binary "@$JsonFile"

$Body = @{
  stream = $true
  max_tokens = 64
  messages = @(@{ role = 'user'; content = '你好' })
} | ConvertTo-Json -Depth 5 -Compress
[System.IO.File]::WriteAllText($JsonFile, $Body, $Utf8NoBom)

curl.exe -N -X POST "$BASE/chat/completions" `
  -H "Authorization: Bearer $KEY" `
  -H "Content-Type: application/json" `
  --data-binary "@$JsonFile"

Remove-Item $JsonFile -ErrorAction SilentlyContinue
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)
Remove-Variable KEY,SecureKey,BSTR,Body,JsonFile,Utf8NoBom
```

再使用无敏感信息的错误 Key 验证鉴权边界：

```powershell
curl.exe -i "$BASE/models" -H "Authorization: Bearer invalid_test_key"
Remove-Variable BASE
```

正确 Key 的 `/models` 必须返回200；错误 Key必须返回401；非流式请求必须返回合法 JSON；SSE必须逐帧输出，包含一个 `finish_reason` 为 `stop` 且带 `usage` 的结束帧，最后输出 `[DONE]`。如果服务返回 `Invalid JSON request body`，但 `/models` 的200/401均正确，通常是 Windows PowerShell 把内联 JSON 或 `Content-Type` 引号拆坏，改用上面的无 BOM UTF-8临时文件，不要修改后端。全部通过后才填写清小搭。若父域名接入备案、DNS或证书仍未完成，按 §9.10 使用 API网关回退。

### 6.5 可选：从自有前端管理 Key

部署前端后，管理员登录 `/login`，进入：

```text
管理 → Agent 接入管理
```

可以：

- 创建并一次性复制新的 Agent API Key；
- 查看 Key 名称和最近使用时间；
- 撤销泄露、停用或不再使用的 Key。

管理员登录令牌、Agent API Key、DeepSeek Key、VLM Key 和 ECS SSH 私钥是五种不同凭证，不能混用。

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
sudo -u paper-agent /opt/paper-agent/.venv/bin/python \
  /opt/paper-agent/scripts/cleanup_openai_api_storage.py --preview
```

先审阅 `preview.actions` 和回收统计：如果 `actions` 为空、`reclaimed_bytes` 为 `0`，立即停止，不需要执行确认步骤。只有确实需要执行预览中的清理动作时，才在 10 分钟内通过隐藏输入粘贴上一步输出的**实际** token；不要把字面量 `<token>` 当成参数，也不要把 token 发到聊天、截图或命令历史：

```bash
read -rsp '粘贴刚生成的 cleanup preview token: ' CLEANUP_TOKEN; echo
sudo -u paper-agent /opt/paper-agent/.venv/bin/python \
  /opt/paper-agent/scripts/cleanup_openai_api_storage.py \
  --execute-token "$CLEANUP_TOKEN"
unset CLEANUP_TOKEN
```

## 8. 可选 Next.js 前端服务

清小搭接入不依赖前端；但推荐部署前端供管理员管理密钥和少量用户直接使用。

构建时把 SSE 直连地址设为同一个公网 Origin：

```bash
cd /opt/paper-agent/frontend
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
```

### 8.1 可选自有前端 systemd

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

若 §3 的 `command -v pnpm` 不是 `/usr/local/bin/pnpm`，必须用实际绝对路径替换 `ExecStart`；不要依赖 systemd 的交互式 shell PATH。`Environment=HOSTNAME=127.0.0.1` 不能可靠约束 `next start`，必须在命令行显式使用 `-H 127.0.0.1`。不要把 `ExecStart` 写成 `/usr/local/bin/pnpm start`：仓库旧版 `start` 脚本只执行 `next start -p 3000`，会监听 `*:3000`。

保存后先核对文件实际内容：

```bash
sudo grep -nE 'ExecStart|HOSTNAME' /etc/systemd/system/paper-agent-web.service
```

预期只出现：

```text
ExecStart=/usr/local/bin/pnpm exec next start -H 127.0.0.1 -p 3000
```

启动或修改后重新加载：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paper-agent-web
sleep 3
sudo systemctl status paper-agent-web --no-pager
sudo systemctl show paper-agent-web -p ExecStart
sudo ss -ltnp | grep ':3000'
curl -I http://127.0.0.1:3000
```

通过标准：服务为 `active (running)`；`systemctl show` 的实际 `ExecStart` 含 `-H 127.0.0.1 -p 3000`；`ss` 明确显示 `127.0.0.1:3000`。如果仍显示 `*:3000`、`0.0.0.0:3000` 或 `[::]:3000`，说明 systemd 仍加载旧命令：执行 `systemctl cat paper-agent-web` 检查来源，修正后再次 `daemon-reload` 和 `restart`。3000 端口始终不得加入公网安全组。

`systemd-analyze verify` 若只报告 `/lib/systemd/system/snapd.service` 的 `Unknown key name 'RestartMode'`，这是 Ubuntu/阿里云镜像中 snapd unit 与当前 systemd 版本的兼容提示，不是 `paper-agent-web.service` 错误；仍以本 unit 状态、实际 `ExecStart` 和监听地址为准。

---

## 9. 主方案：授权子域名 → 北京 ECS → Nginx HTTPS

### 9.0 启用主方案前的硬门槛

继续之前必须确认：

- 域名所有者已经添加 A记录，值为当前北京 ECS公网 IPv4；
- 父域名已经 ICP备案；
- 父域名已接入阿里云，或备案主体已完成阿里云接入备案；
- 域名所有者明确授权该子域名用于当前服务；
- 安全组80/443已开放，22仍只允许管理员 `/32`；
- 8000/3000没有公网规则。

任何一项无法确认，停止主方案并跳到 §9.10 API网关回退，不要把“DNS能解析”误当作备案/授权完成。

### 9.1 设置实际域名并验证 A记录

```bash
DOMAIN='paper-agent.example.com'
PUBLIC_IP='123.123.123.123'
ADMIN_EMAIL='your-email@example.com'

getent ahostsv4 "$DOMAIN"
dig +short A "$DOMAIN"
```

结果必须包含 `PUBLIC_IP`。继续检查：

```bash
dig +short CAA "$DOMAIN"
dig +short CAA "${DOMAIN#*.}"
```

如果 CAA不允许计划使用的 CA，联系域名所有者调整。不要在 DNS尚未生效时连续重复申请证书，避免触发 CA频率限制。

### 9.2 创建 HTTP引导站点

先创建 Web根目录：

```bash
sudo mkdir -p /var/www/paper-agent-acme
sudo chown -R www-data:www-data /var/www/paper-agent-acme
```

创建 `/etc/nginx/sites-available/paper-agent`，把示例域名替换成实际值：

```nginx
server {
    listen 80;
    server_name paper-agent.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/paper-agent-acme;
    }

    location / {
        default_type text/plain;
        return 200 'Paper Agent HTTPS bootstrap\n';
    }
}
```

启用：

```bash
sudo ln -s /etc/nginx/sites-available/paper-agent \
  /etc/nginx/sites-enabled/paper-agent
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

从自己电脑测试：

```bash
curl -i "http://$DOMAIN/"
```

必须到达北京 ECS，而不是旧服务器、CDN或域名停车页。

### 9.3 签发 HTTPS证书

#### 方法 A：Certbot HTTP验证（推荐）

```bash
sudo certbot --nginx \
  -d "$DOMAIN" \
  --redirect \
  --agree-tos \
  --no-eff-email \
  -m "$ADMIN_EMAIL"
```

成功后证书通常位于：

```text
/etc/letsencrypt/live/<实际域名>/fullchain.pem
/etc/letsencrypt/live/<实际域名>/privkey.pem
```

检查自动续期：

```bash
sudo systemctl status certbot.timer --no-pager
sudo certbot renew --dry-run
```

`renew --dry-run` 使用 Let's Encrypt 测试环境模拟续期；失败不会删除、替换或吊销当前正式证书。若报 `Timeout during connect (likely firewall problem)`，不要连续重试，按顺序检查：

```bash
# 证书仍然存在且查看当前有效期（不输出私钥）
sudo certbot certificates
sudo openssl x509 -in /etc/letsencrypt/live/paper-agent.ycr10.cn/fullchain.pem \
  -noout -subject -issuer -dates

# nginx 是否对公网监听80/443；后端端口仍应只在回环地址
sudo ss -ltnp | grep -E ':80|:443|:3000|:8000'
sudo nginx -t
sudo systemctl status nginx --no-pager
sudo ufw status verbose
```

HTTP-01 会从多个、会变化的验证地址访问 TCP 80，不能只允许管理员 IP，也不能试图维护 Let’s Encrypt 出口 IP 白名单。阿里云安全组必须为 `80/TCP` 和 `443/TCP` 设置来源 `0.0.0.0/0`；22仍只允许管理员 `/32`，3000/8000不开放。若 UFW 已启用，也必须允许80/443。

在 ACME Web 根创建无敏感内容的探测文件。下面两条命令必须在 **ECS SSH终端**执行；这里的 `127.0.0.1` 指ECS自身：

```bash
sudo mkdir -p /var/www/paper-agent-acme/.well-known/acme-challenge
echo 'acme-probe-ok' | sudo tee \
  /var/www/paper-agent-acme/.well-known/acme-challenge/probe >/dev/null
curl -i http://127.0.0.1/.well-known/acme-challenge/probe \
  -H 'Host: paper-agent.ycr10.cn'
```

再从自己的 **Windows PowerShell** 执行公网测试。必须写 `curl.exe`，避免 Windows PowerShell 5.1 把 `curl` 解析成 `Invoke-WebRequest`；也不要在自己电脑使用 `127.0.0.1`，那会访问自己的电脑而非ECS：

```powershell
curl.exe -i http://paper-agent.ycr10.cn/.well-known/acme-challenge/probe
```

如需绕过本机DNS缓存并强制测试当前ECS公网IP，同时保持正确 Host，可执行：

```powershell
curl.exe -i --resolve paper-agent.ycr10.cn:80:123.57.6.126 http://paper-agent.ycr10.cn/.well-known/acme-challenge/probe
```

必须直接返回 `200` 和 `acme-probe-ok`。如果挑战路径返回 `301`，说明当前启用的80端口 `server` 块仍在 server 级执行全局 `return 301`，或没有包含挑战 location；不要依赖重定向掩盖配置错误。将80端口块改为 `location ^~ /.well-known/acme-challenge/` 直接提供文件，并只在 `location /` 内执行 HTTPS 跳转。外部探测通过后删除测试文件，再只重试一次：

```bash
sudo rm -f /var/www/paper-agent-acme/.well-known/acme-challenge/probe
sudo certbot renew --dry-run
```

如果自己的电脑可访问而 dry-run仍连接超时，优先检查阿里云安全组是否把80错误限制为个人IP、是否存在云防火墙/WAF/CDN地域限制；HTTP-01要求验证节点能从不同地区访问。无法满足全球80入口时，改用由域名所有者配合的 DNS-01，不要关闭证书续期告警后放任证书过期。

#### 方法 B：域名所有者提供证书

如果域名所有者提供单域名/通配符证书：

```bash
sudo mkdir -p /etc/nginx/ssl/paper-agent
sudo install -o root -g root -m 600 fullchain.pem \
  /etc/nginx/ssl/paper-agent/fullchain.pem
sudo install -o root -g root -m 600 privkey.pem \
  /etc/nginx/ssl/paper-agent/privkey.pem
```

证书必须覆盖实际二级域名且未过期。不要通过公开聊天、Git或工单传私钥。

### 9.4 配置正式 Nginx HTTPS和 SSE

Certbot签发成功后，将 `/etc/nginx/sites-available/paper-agent` 改为下面配置。证书路径换成实际域名路径；如果使用所有者提供的证书，换成 `/etc/nginx/ssl/paper-agent/...`。

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name paper-agent.example.com;

    # HTTP-01 必须直接从80端口取到挑战文件；不要把 return 写在 server 级，
    # 否则该 return 会先于 location 选择执行，使挑战路径也被重定向。
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
    server_name paper-agent.example.com;

    ssl_certificate     /etc/letsencrypt/live/paper-agent.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/paper-agent.example.com/privkey.pem;
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
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }

    location /elements/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Guest-Id $http_x_guest_id;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8000/health;
    }

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

检查并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

不要在 `/v1` 或 `/files` 上配置 Basic Auth，它会破坏清小搭 Bearer鉴权和附件转存。

### 9.5 更新后端环境变量

```bash
cd /opt/paper-agent
sudo -u paper-agent nano .env
```

设置为实际子域名：

```ini
PUBLIC_BASE_URL=https://paper-agent.example.com
FRONTEND_ORIGIN=https://paper-agent.example.com
CORS_ORIGINS=
```

不带 `/v1`、不带尾部 `/`。然后：

```bash
sudo systemctl restart paper-agent
sudo systemctl status paper-agent --no-pager
```

### 9.6 按实际域名重新构建前端

Next.js的 `NEXT_PUBLIC_BACKEND_URL` 在构建时注入，换域名后必须重新构建：

```bash
cd /opt/paper-agent/frontend
sudo -H -u paper-agent env \
  HOME=/var/lib/paper-agent \
  BACKEND_URL=http://127.0.0.1:8000 \
  NEXT_PUBLIC_BACKEND_URL=https://paper-agent.ycr10.cn \
  /usr/local/bin/pnpm build
sudo systemctl restart paper-agent-web
```

如果只接清小搭、不部署自有前端，可以跳过本节并让 Nginx `/` 返回404。

### 9.7 验证 DNS、证书和 HTTPS

从自己的电脑执行。Windows PowerShell 必须使用 `curl.exe`，不要使用会被解析为 `Invoke-WebRequest` 的 `curl` 别名：

```powershell
curl.exe -I http://paper-agent.ycr10.cn/
curl.exe -i https://paper-agent.ycr10.cn/health
curl.exe -I https://paper-agent.ycr10.cn/chat
```

证书详细信息可在 ECS 执行：

```bash
openssl s_client -connect paper-agent.ycr10.cn:443 \
  -servername paper-agent.ycr10.cn </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

期望：

- HTTP返回301跳HTTPS；
- `/health` 返回200；
- 证书SAN覆盖实际域名；
- 证书未过期且链完整；
- 浏览器不出现证书警告。

### 9.8 验证清小搭 OpenAI兼容接口

Windows客户端按 §6.4 的 PowerShell命令完成正式公网验收；下面是 Linux/macOS客户端的等价命令：

```bash
read -rsp 'Agent API Key: ' KEY; echo
BASE='https://paper-agent.ycr10.cn/v1'

curl -i "$BASE/models" -H "Authorization: Bearer $KEY"

curl -sS -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"只回复：非流式成功"}]}'

curl -N -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"stream":true,"max_tokens":64,"messages":[{"role":"user","content":"你好"}]}'

unset KEY
```

要求：正确Key 200、错误Key 401、SSE逐帧、stop含usage、最后 `[DONE]`。

### 9.9 验证论文来源并记录启用清单

从北京 ECS执行至少三轮：

```bash
for round in 1 2 3; do
  echo "===== round $round ====="
  for url in \
    'https://api.openalex.org/works?search=graph%20neural%20network&per-page=1' \
    'https://export.arxiv.org/api/query?search_query=all:graph%20neural%20network&max_results=1' \
    'https://api.semanticscholar.org/graph/v1/paper/search?query=graph%20neural%20network&limit=1&fields=title' \
    'https://api.crossref.org/works?query=graph%20neural%20network&rows=1'
  do
    curl -4 -L --connect-timeout 10 --max-time 30 -sS -o /dev/null \
      -w '%{http_code} connect=%{time_connect}s total=%{time_total}s %{url_effective}\n' "$url"
  done
  sleep 10
done
```

结果判定：

- `200`：该来源从当前 ECS 公网出口稳定可用；
- `401/403`：网络可达，但凭证或来源权限不满足；
- `429`：网络可达，不是“外网被阻断”，而是该来源正在限流；
- `000`、DNS失败或持续连接超时：才按网络不可达排查。

Semantic Scholar 未认证请求共享公共额度，繁忙时可能连续 `429`。优先申请 API Key 并仅写入生产 `.env` 的 `S2_API_KEY`；Key 不得进入命令历史、文档或 Git。官方当前为新 Key 提供的起始额度为 1 req/s，本项目的每源限流和 429 重试仍须保留。如果演示前仍无 Key 且三轮都为 `429`，不要把 Semantic Scholar 作为关键依赖；OpenAlex、arXiv、Crossref 等稳定来源继续工作，搜索管理器会保留部分结果并降级。

只将多轮稳定来源作为比赛/演示依赖；单个来源失败时保留多源降级和摘要回退。不要通过校园VPN、代理或订阅账号自动拉取付费全文。

---

### 9.10 先确认：哪一种阿里云“内网穿透”可用

阿里云官方能力中：

- **云助手会话管理/端口转发**：不能用于清小搭。它要求管理员本机运行 `ali-instance-cli` 并保持会话，只生成本机 `localhost` 端口映射，没有稳定公网 HTTPS 根地址；
- **NAT SNAT**：只能让 ECS 主动出网，清小搭不能入站；
- **NAT DNAT/EIP/固定公网 IP/ALB**：能提供公网入口，但大陆自有域名仍需 ICP；
- **传统 API 网关默认公网二级域名 + VPC 授权**：可以把北京 VPC 内 ECS 私网服务发布成阿里云默认 HTTPS API 地址，是本文前三天的条件可行方案。

该方案不是正式备案替代品。默认域名只供测试，必须在三天内实际验证清小搭是否接受额外 `Content-Disposition` 响应头。

### 9.11 在 ECS 配置只供 API 网关访问的 Nginx 8080

FastAPI 仍只监听 `127.0.0.1:8000`。创建 `/etc/nginx/sites-available/paper-agent-internal`：

```nginx
server {
    listen 8080;
    server_name _;
    client_max_body_size 25m;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Content-Type $http_content_type;
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
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8000/health;
    }

    location / {
        return 404;
    }
}
```

启用并测试：

```bash
sudo ln -s /etc/nginx/sites-available/paper-agent-internal \
  /etc/nginx/sites-enabled/paper-agent-internal
sudo nginx -t
sudo systemctl reload nginx

curl -i http://127.0.0.1:8080/health
curl -i http://127.0.0.1:8080/v1/models
```

`/health` 应返回 200；`/v1/models` 没有 Bearer Key 时返回 401，说明路由存在且鉴权没有被绕过。

### 9.12 选择 API 网关实例类型

进入阿里云控制台，搜索 **“API 网关”**，选择 **“传统 API 网关”**，地域切换到 **华北 2（北京）**。

| 需求 | 选择 | 官方后端超时上限 |
|---|---|---:|
| 只通过清小搭探测、短对话 | Serverless 实例 | 60 秒 |
| 展示论文检索、深读、OCR、长 SSE | 专享实例（最小规格起步） | 3600 秒 |

本项目工具任务经常超过 60 秒，因此若三天演示包含全文深读/OCR，直接购买北京按量专享实例 `api.s1.small` 起步；不要把 Serverless超时误判为后端故障。专享实例也要保持 API请求量在默认二级域名的 1000 次/天测试限制内。

### 9.13 创建 API 分组并复制默认域名

路径：

```text
传统 API 网关
→ API 管理
→ 分组管理
→ 创建分组
```

填写：

```text
分组名称：paper-agent-temp
实例：选择刚创建的北京 Serverless/专享实例
BasePath：空或 /
```

创建后复制控制台显示的默认公网二级域名，形如：

```text
https://<group-id>-cn-beijing.alicloudapi.com
```

这是三天临时阶段的公网 Origin。API 网关默认二级域名不带 `X-Ca-Stage` 时调用 **RELEASE/线上环境**；清小搭不会为本项目额外发送 `X-Ca-Stage`，因此后续 API必须发布到 RELEASE。TEST/PRE 只用于手工调试。

### 9.14 创建 VPC 授权

路径：

```text
传统 API 网关
→ API 管理
→ VPC 授权
→ 创建授权
```

填写：

```text
授权名称：paper-agent-beijing-vpc
地域：华北 2（北京）
VPC：ECS 所在 VPC
后端资源：ECS实例 ID，或 ECS 私网 IPv4
端口：8080
```

API 网关、ECS和 VPC必须同地域。创建后记录页面显示的 **API 网关出口 IP**；ECS 安全组需要允许这些 IP访问 8080。

### 9.15 收紧安全组 8080

进入：

```text
ECS → 网络与安全 → 安全组 → paper-agent-sg → 入方向 → 手动添加
```

为控制台列出的每个 API 网关出口 IP添加：

```text
协议：TCP
端口：8080
来源：网关出口IP/32
优先级：1
```

禁止：

```text
0.0.0.0/0:8080
0.0.0.0/0:8000
0.0.0.0/0:3000
```

临时阶段不需要全网 80/443。ECS 公网 IP只保留 SSH `/32` 和默认出网能力。

### 9.16 开启 API 网关 SSE

进入：

```text
分组管理 → paper-agent-temp → 分组详情
→ 数据传输设置 → 修改配置
→ 开启“支持流式数据传输”
```

开启后网关才会按 SSE实时转发，不缓存完整响应。不要给这些 API配置响应缓存或错误码映射插件。

### 9.17 创建清小搭必需 API

每个 API都选择：

```text
安全认证：无认证
请求参数模式：入参透传
后端服务类型：VPC
VPC授权：paper-agent-beijing-vpc
后端协议：HTTP
后端端口：8080
```

“无认证”只是关闭 API 网关自己的 AppKey；Paper Agent 仍通过 `Authorization: Bearer pa_live_...` 鉴权。`入参透传`必须保留 `Authorization`、`Content-Type` 和 JSON body。

至少创建：

| API名称 | 前端方法/路径 | 后端路径 | 用途 |
|---|---|---|---|
| `paper-agent-models` | `GET /v1/models` | `/v1/models` | 清小搭连通和凭证探测 |
| `paper-agent-chat` | `POST /v1/chat/completions` | `/v1/chat/completions` | 非流式和 SSE 对话 |
| `paper-agent-file` | `GET /files/[filename]` | `/files/[filename]` | 清小搭附件下载 |
| `paper-agent-health` | `GET /health` | `/health` | 运维检查 |

创建 `paper-agent-chat` 时：

- Serverless 后端超时设为允许的最大值 60 秒；
- 专享实例设为覆盖真实任务的值，最长不超过 3600 秒；
- 请求 body透传，不要定义会丢弃未知字段的固定映射；
- 响应不要启用 JSON包装、缓存或错误码转换。

`/files/[filename]` 的前后端路径参数必须同名透传；否则 `x_soda.attachments.fileUrl` 会返回 404。当前 Qingxiaoda不依赖 `/api/v1` 或前端 React 路由，临时网关不用暴露管理员页面。

### 9.18 发布到 RELEASE 并设置 PUBLIC_BASE_URL

将四个 API发布到 **RELEASE/线上环境**。然后修改 `/opt/paper-agent/.env`：

```ini
PUBLIC_BASE_URL=https://<group-id>-cn-beijing.alicloudapi.com
```

不带 `/v1`。重启后端：

```bash
sudo systemctl restart paper-agent
sudo systemctl status paper-agent --no-pager
```

### 9.19 从公网验证默认域名

```bash
read -rsp 'Agent API Key: ' KEY; echo
BASE='https://xxxxxxxx-cn-beijing.alicloudapi.com/v1'

curl -i "$BASE/models" -H "Authorization: Bearer $KEY"

curl -sS -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"只回复：非流式成功"}]}'

curl -N -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"stream":true,"max_tokens":64,"messages":[{"role":"user","content":"你好"}]}'

unset KEY
```

检查：

- `/models` HTTP 200；
- 错误 Key 401；
- 非流式包含 `choices[0].message.content`；
- 流式逐帧出现，最后 `[DONE]`；
- `Authorization` 没有被网关吞掉；
- 网关额外 `Content-Disposition` 没有改变 JSON/SSE body；
- 请求超过所选实例超时上限时能够明确复现。

### 9.20 默认域名限制与失败判定

默认公网二级域名只允许测试：

- 中国大陆地域每日最多 1000 次调用；
- 响应统一增加 `Content-Disposition: attachment`；
- 不带 `X-Ca-Stage` 时默认访问 RELEASE；清小搭接入不能依赖自定义 `X-Ca-Stage`；
- Serverless 后端超时最多 60 秒；
- Serverless 请求 body上限 8 MB，专享实例 32 MB；
- 专享实例虽可将超时提高到 3600 秒，但默认域名仍不是正式生产域名。

如果清小搭因为 `Content-Disposition` 拒绝解析，或者平台网络无法访问默认域名，则“API 网关临时入口”不满足清小搭，不能继续用更换 EIP/NAT/云助手端口转发冒充解决；应改用已备案域名或非大陆公网入口。

---

## 10. 按清小搭“标准协议接入”页面填写

### 10.1 主路线填写

在清小搭“智能体接入”：

```text
平台：标准协议接入
API地址：https://paper-agent.ycr10.cn/v1
API密钥：pa_live_...
鉴权方式：Bearer Token
流式终止符：[DONE]
usage位置：stop帧内
流式能力：开启
模型字段：留空或 paper-agent
```

API地址只到 `/v1`，不要填 `/v1/chat/completions`；当前正式地址固定为 `https://paper-agent.ycr10.cn/v1`。

### 10.2 清小搭测试预期

- `GET /v1/models`：正确Key 200，错误Key 401；
- `POST /v1/chat/completions`：非流式有 `choices[0].message.content`；
- SSE首帧 `delta.role=assistant`；
- 后续 `delta.reasoning`/`delta.content`；
- stop帧含 `finish_reason` 和 `usage`；
- 最后 `data: [DONE]`；
- `x_soda.attachments.fileUrl` 使用同一 HTTPS域名且可下载。

### 10.3 主路线常见失败

| 现象 | 检查 |
|---|---|
| 域名无法连接 | A记录是否指向当前公网IP、是否仍被CDN代理、80/443安全组 |
| 证书错误 | SAN是否覆盖子域名、CAA、证书链和过期时间 |
| 401 | `pa_live_` Key是否完整，Nginx是否透传Authorization |
| 404 | API地址是否误填完整 `/chat/completions`，Nginx `/v1/` 是否存在 |
| 502 | FastAPI systemd状态和127.0.0.1:8000健康检查 |
| SSE一次性出现 | `proxy_buffering off`、`gzip off`、无CDN缓存 |
| 附件地址错误 | `PUBLIC_BASE_URL` 是否为实际HTTPS Origin且不带 `/v1` |
| 前端连接旧域名 | 修改 `NEXT_PUBLIC_BACKEND_URL` 后重新 `pnpm build` |

### 10.4 回退 API网关填写

仅在子域名备案/接入/DNS/证书未完成时使用：

```text
API地址：https://xxxxxxxx-cn-beijing.alicloudapi.com/v1
API密钥：同一个 pa_live_...
```

回退步骤见 §9.10及以后。默认二级域名只供测试，存在1000次/天、`Content-Disposition`和网关超时限制；子域名主路线恢复后及时将清小搭地址切回正式域名。

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

本次经济型实例建议只为 1–3 位固定用户开户。可在受控时间窗口临时设置 `REGISTRATION_OPEN=true`，创建完账号后立即恢复 `false` 并重启后端。不要开放游客来替代账号管理。

---

## 12. 上线验收清单

### 协议与鉴权

- [ ] 正确 Agent Key 调 `/v1/models` 返回 200；错误/已撤销 key 返回 401。
- [ ] `stream:"false"`、`stream:1`、`stream:null` 返回 422；JSON `false/true` 正常。
- [ ] 流式首帧 role、后续 reasoning/content、stop+usage、`[DONE]` 顺序正确。
- [ ] 清小搭四项探测全绿，真实试聊成功。
- [ ] 主路线 DNS A记录指向当前 ECS公网IP，父域名备案/阿里云接入和使用授权已核验。
- [ ] HTTPS证书覆盖实际子域名、证书链完整、自动续期或到期提醒可用。
- [ ] `PUBLIC_BASE_URL`、`FRONTEND_ORIGIN`、前端构建时 `NEXT_PUBLIC_BACKEND_URL` 都使用实际 HTTPS子域名。
- [ ] 如果启用API网关回退：默认域名调用量低于1000次/天、API发布到RELEASE、SSE和请求透传已验证，`Content-Disposition` 不影响清小搭。
- [ ] 管理员页面能创建/一次性显示/撤销密钥；普通用户访问返回 403。
- [ ] 云服务器工作树不含开发机 `history_record/`、`data/`、`backend/data/`、旧 PDF、旧上传或旧数据库；首次启动后这些目录才由生产实例新建。

### 前端与文件

> 主路线通过同一授权子域名暴露清小搭 API、受认证 Web API、附件和 Next.js前端；如果只使用API网关回退，则自有前端项目暂缓验收。

- [ ] `GUEST_ACCESS=false` 时未登录访问 `/chat` 被送到 `/login`。
- [ ] 从真实前端调用真实 LLM，思考折叠区持续流式显示；只隐藏内部工具调用机制，不删除正常思考内容。
- [ ] 上传 PNG/JPG/WebP、PDF 和含图片 DOCX 后可预览/按需理解；VLM 缺失时明确降级。
- [ ] 深读在线论文与用户上传文件走同一多模态结构解析/VLM/RAG 缓存路径。
- [ ] 导出长中文文件名 `.docx` 可下载并是有效 OOXML ZIP；同时抽查 md/txt/tex。
- [ ] 清小搭响应中的附件 URL 是公网 HTTPS 且可直接下载。
- [ ] research_map 在清小搭侧出现 Markdown 文件卡和静态 SVG 图像文件卡；不要把自有 React 卡片是否出现作为协议验收项。

### 容量与安全

- [ ] 完成 §0.2 两轮三路混合验收，无 OOM/重启/断流；重型 OCR/深读并发保持 1。
- [ ] 主路线：22仅管理员 `/32`，80/443全网，8000/3000未开放；8080只有启用API网关回退时才按出口IP `/32` 放行。
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

经济型 e 的首要升级信号通常是 CPU 稳定性，而不是内存：

- **先升级到通用型 g 4C16G**：内存仍低于 13 GiB，但同一份 PDF 深读耗时波动明显、OCR 时普通 SSE 经常被拖慢、CPU 长时间满载，或重任务并发 1 仍不能满足体验；
- **再升级到 8C32G**：需要多份扫描 PDF/OCR/深读重叠，或内存长期 >13 GiB、Swap 持续增长；
- **不要直接多 worker**：先外置 Redis/任务队列/共享限流与会话状态，再设计多进程；
- 数据持续增长时优先扩系统盘并制定文件生命周期，不要只依赖清理缓存。

---

## 14. 常见问题

**域名所有者问“需要给什么信息”**：只提供完整二级域名名称、ECS公网IPv4、A记录、默认线路和TTL 600；不要提供私网IP、SSH私钥、管理员密码或任何API Key。

**子域名解析生效但北京 ECS仍打不开**：先确认父域名ICP状态和阿里云接入备案，不要只排查DNS；然后检查80/443安全组、UFW、Nginx和证书。

**Certbot证书签发失败**：检查A记录、80端口、CAA和域名所有权；借用子域名需要域名所有者配合TXT/CNAME验证。不要反复申请触发CA限额。

**API 网关默认域名能打开但清小搭解析失败**：检查响应是否被默认 `Content-Disposition: attachment` 影响。该头无法用“云助手端口转发”或 EIP解决；若清小搭拒绝，临时默认域名方案不可用。

**API 网关请求约 60 秒中断**：Serverless 后端超时上限是 60 秒。短对话继续用 Serverless；全文深读/OCR改用北京专享实例 `api.s1.small` 并把后端超时调高，最大 3600 秒。

**清小搭返回 401**：检查是否粘贴了完整 Agent API Key、是否已撤销，以及 Bearer 前后是否混入额外字符。不要拿 DeepSeek Key 代替。

**清小搭返回 503**：生产环境没有任何 Agent API Key。先运行 §6 的交互式脚本或从管理员页面创建。

**`paper-agent.service` 反复重启，日志提示 `Form data requires "python-multipart"`**：上传接口依赖没有完整安装。先停止重启循环，更新到包含 `backend/pyproject.toml` 和 `constraints.txt` 修复的代码，再重跑标准安装命令：

```bash
sudo systemctl stop paper-agent
cd /opt/paper-agent
sudo -H -u paper-agent .venv/bin/pip install -r requirements.txt -e backend
sudo -H -u paper-agent .venv/bin/python -m pip check
sudo systemctl reset-failed paper-agent
sudo systemctl start paper-agent
curl -i http://127.0.0.1:8000/health
```

不要只在服务器永久保留一次手工 `pip install` 而不修复源代码依赖，否则下次新服务器或重建 `.venv` 会再次失败。

**SSE 一次性整段出现**：确认 nginx `/v1/` 和 `/api/v1/` 已 `proxy_buffering off`、`X-Accel-Buffering: no`，并确认中间 CDN 没有缓存流。

**附件 URL 指向内网或 HTTP**：临时阶段把 `PUBLIC_BASE_URL` 设为 API 网关默认 HTTPS Origin（不带 `/v1`），并确认已创建 `/files/[filename]` API；备案阶段再换成自有域名。

**PDF 首次深读很慢**：Docling 首次模型加载和 CPU OCR 本来就重；先确认 §4.1 已由服务账号预热。经济型 e 使用共享、非绑定 CPU，同一任务耗时可能波动；重任务并发保持 1。如果普通 SSE 仍经常被拖慢，升级通用型 g 4C16G，不要增加 Uvicorn worker。

**管理员密码忘记**：当前初始化脚本不会重置已有密码，这是防止部署脚本意外接管账号。应走受控的离线恢复流程或备份恢复，不要删除整个用户库。

---

## 参考

### 阿里云官方文档（本手册外部依据）

以下页面均为阿里云官方帮助中心；控制台和在售规格会变化，购买当天仍应以购买页库存、兼容性提示和费用明细为准：

- [使用向导创建 ECS 实例](https://help.aliyun.com/zh/ecs/user-guide/create-an-instance-by-using-the-wizard/)
- [实例规格族概述](https://help.aliyun.com/zh/ecs/user-guide/overview-of-instance-families)
- [通用算力型实例规格（U 实例）](https://help.aliyun.com/zh/ecs/user-guide/general-work-force)
- [计算型实例规格（c 系列）](https://help.aliyun.com/zh/ecs/user-guide/compute-optimized-instance-families)
- [通用型实例规格（g 系列）](https://help.aliyun.com/zh/ecs/user-guide/general-purpose-instance-families)
- [共享型/经济型/突发性能实例规格](https://help.aliyun.com/zh/ecs/user-guide/shared-instance-families)
- [安全组规则](https://help.aliyun.com/zh/ecs/user-guide/security-group-rules)
- [避免使用弱口令登录 ECS 实例](https://help.aliyun.com/zh/ecs/user-guide/avoid-using-a-weak-password-to-log-in-to-an-instance)
- [ESSD 云盘](https://help.aliyun.com/zh/ecs/user-guide/essds)
- [公网带宽计费方式](https://help.aliyun.com/zh/ecs/user-guide/public-bandwidth)
- [为 ECS 配置公网访问能力](https://help.aliyun.com/zh/ecs/user-guide/configure-public-network-access)
- [地域和可用区](https://help.aliyun.com/zh/ecs/user-guide/regions-and-zones)
- [使用 VPC 和交换机创建 ECS](https://help.aliyun.com/zh/vpc/user-guide/vpc-and-vswitch)
- [添加网站解析（A 记录）](https://help.aliyun.com/zh/dns/pubz-add-website-parsing)
- [ICP备案流程与备案后处理](https://help.aliyun.com/zh/icp-filing/basic-icp-service/user-guide/icp-filing-application-overview)
- [源站在阿里云中国内地时的备案接入要求](https://help.aliyun.com/zh/icp-filing/basic-icp-service/support/faq-about-icp-filing-preparations)
- [公安联网备案信息填写](https://help.aliyun.com/zh/icp-filing/basic-icp-service/the-public-security-network-for-the-record-information-fill-in-the-guide)
- [SSL证书域名所有权验证方式](https://help.aliyun.com/zh/ssl-certificate/select-a-method-for-domain-name-verification)
- [二级域名DNS/TXT所有权验证](https://help.aliyun.com/zh/ssl-certificate/frequently-asked-questions-on-domain-ownership-verification)
- [在Nginx部署SSL证书](https://help.aliyun.com/zh/ssl-certificate/install-ssl-certificates-on-nginx-servers-or-tengine-servers)
- [备案服务器校验与购买要求](https://help.aliyun.com/zh/icp-filing/basic-icp-service/support/faq-cloud-services-and-ip-address)
- [ECS 实例释放保护](https://help.aliyun.com/zh/ecs/user-guide/enable-or-disable-release-protection-for-ecs-instances)
- [设置 ECS 实例报警规则](https://help.aliyun.com/zh/ecs/user-guide/configure-alerts-for-an-ecs-instance)
- [使用 Workbench 登录 Linux 实例](https://help.aliyun.com/zh/ecs/user-guide/connect-to-a-linux-instance-by-using-a-password-or-key)
- [云助手会话管理 CLI 端口转发](https://help.aliyun.com/zh/ecs/user-guide/perform-port-forwarding-by-using-ali-instance-cli)
- [API 网关访问 VPC 内 ECS 后端](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/getting-started/create-an-api-operation-with-a-resource-in-a-vpc-as-the-backend-service)
- [API 网关默认二级域名与自有域名限制](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/user-guide/bind-a-domain-name-to-an-api-group)
- [API 网关使用限制](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/product-overview/limits)
- [API 网关 SSE 流式数据传输](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/user-guide/support-streaming-data-transfer-sse/)
- [API 网关 Serverless/专享实例与北京地域支持](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/product-overview/instance-types)
- [API 网关环境与 X-Ca-Stage](https://help.aliyun.com/zh/api-gateway/traditional-api-gateway/user-guide/configure-different-environments-for-an-api-operation)

### 本仓库与其他组件

- 本仓库：`openai-compatible-agent-integration-guide.md`
- 本仓库：`README.md`、`docs/DESIGN.md`
- Next.js Self-hosting 官方文档
- nginx Reverse Proxy 官方文档
