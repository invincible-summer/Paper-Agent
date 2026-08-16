# 自研 Agent 接入清小搭广场 · 开发者指南（OpenAI 兼容协议）

> 读者：① 想把自研 agent 接入清小搭智能体广场的**开发者**；② 帮开发者改造 / 生成服务的 **AI coding agent**。
> 目标：照本文实现一个服务端，即可通过接入向导的「探测」与「试聊」，被广场零代码接入。
> 关联：OpenAI 兼容协议规范、接入探测规则、内置 mock 上游服务。

---

## 0. 速览（最快接入）

把你的 agent 暴露成一个 **OpenAI 兼容的 HTTP 服务**，提供两个端点、用 Bearer 鉴权，即可接入：

| 端点 | 必须 | 用途 |
|---|---|---|
| `POST {baseUrl}/chat/completions` | ✅ 必须 | 对话（流式 SSE + 非流式 JSON） |
| `GET {baseUrl}/models` | 🟡 强烈建议 | 连通性 / 凭证校验（缺失可被最小对话兜底） |

接入向导主要填 2 个值即可：`baseUrl`（填到版本段，如 `https://your.host/v1`）、`credential`（你的密钥）。

> 你**不需要**实现平台网关的任何东西——网关由清小搭提供。你只需做到「响应长得像 OpenAI」。私有协议（如自研用了 Coze/Dify 的非 OpenAI 结构）才需要专属适配，普通自研服务走本指南即可。

### 0.1 接入向导怎么走

开发者在「标准协议接入」里按 4 步完成上架：

```text
1. 平台信息
   选择「标准协议接入」→ 填 API 地址 / API 密钥 → 必要时展开高级配置

2. 测试验证
   平台自动探测 /models 与 /chat/completions → 展示 4 项检查结果
   探测通过后可直接试聊，验证真实回复效果

3. 完善信息
   填头像、智能体名称、描述、开场白、上架分类、引导问题

4. 审核上线
   提交审核 → 审核通过后上架广场
```

第 2 步如果出现红叉，优先看失败项：`凭证校验` 对应密钥/鉴权方式，`发起最小对话` 对应 `/chat/completions`，`校验响应格式` 对应 OpenAI 兼容响应结构。

---

## 1. 最小契约（L0，必须满足）

你的服务必须满足以下硬性要求，否则探测不通过：

1. **协议**：HTTP/HTTPS，`Content-Type: application/json`；流式响应为 `text/event-stream`。
2. **鉴权**：支持 `Authorization: Bearer <credential>`（或 `x-api-key` / 自定义头，见 §6）。无效凭证返回 `401`。
3. **对话端点**：`POST {baseUrl}/chat/completions`，接受 OpenAI 风格请求，返回 OpenAI 风格响应（§3）。
4. **流式**：`stream:true` 时返回 SSE，以 `data: [DONE]` 结尾（§3.2）。
5. **finish_reason**：只用官方 5 值之一（§4.1）。
6. **usage**：返回 token 用量字段（§4.2），无法统计时填 0。

> `baseUrl` 约定：填到版本段为止（如 `.../v1`），网关用 `baseUrl + /chat/completions`、`baseUrl + /models` 拼接，不做 `/v1` 去重。

---

## 2. 端点一：`GET {baseUrl}/models`（连通与凭证校验）

用于探测连通性与凭证是否有效。它是 OpenAI 兼容协议的常见端点，建议实现；如果暂时不实现，也可以依赖最小对话兜底。

**请求**：
```
GET {baseUrl}/models
Authorization: Bearer <credential>
```

**响应（200）**：
```jsonc
{
  "object": "list",
  "data": [
    { "id": "default", "object": "model", "owned_by": "you" }
  ]
}
```

要点：
- 凭证无效 → 返回 `401`（探测据此判 `credential` 失败）。
- 不实现该端点也能接入：探测会继续用最小对话兜底。你的核心工作仍然是让 `/chat/completions` 可调用、可返回 OpenAI 兼容结构。

---

## 3. 端点二：`POST {baseUrl}/chat/completions`（对话）

**请求体（你需要接受的字段）**：
```jsonc
{
  "messages": [
    { "role": "system",  "content": "..." },
    { "role": "user",    "content": "你好" }
  ],
  "stream": false,          // 或 true
  "max_tokens": 1024        // 探测会发 max_tokens:1，需能接受
}
```
- 至少支持 `role ∈ {system, user, assistant}`；`content` 为字符串。
- 必须**严格按 JSON 布尔**解析 `stream`（不要把字符串 `"false"` 当真）。
- `model` 可能缺失、为空或为 `null`；普通自研 agent 直接忽略即可。

### 3.1 非流式响应（`stream:false`）

```jsonc
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1735689600,
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "完整回答" },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20 }
}
```
> 探测判定「OpenAI 兼容」的关键：响应里能取到 `choices[0].message.content`（非流式）或 `choices[0].delta`（流式）。
>
> 关于响应里的 `model` 字段：探测与网关**不强校验**，也**不会用它做路由或能力判断**。普通自研 agent 可以不返回，或返回固定值。

### 3.2 流式响应（`stream:true`，SSE）

`Content-Type: text/event-stream`，帧之间用 `\n\n` 分隔，**严格按以下顺序**：

```
data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"choices":[{"index":0,"delta":{"content":"巴"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"choices":[{"index":0,"delta":{"content":"黎"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14}}

data: [DONE]
```

帧序列规范：
1. **role 帧**（恰好一次，首帧）：`delta:{"role":"assistant"}`
2. **content 帧**（0..N 次）：`delta:{"content":"增量文本"}`
3. **stop 帧**（恰好一次）：`delta:{}` + `finish_reason:"stop"`，usage 建议合并在此帧
4. **`data: [DONE]`**（必须，终止哨兵）

> 探测会发 `stream:true` 的最小对话：**收到任意 `data:` SSE chunk** 即判定 `streaming=true（verified）`；若你忽略 stream 返回整段 JSON，则 `streaming=false`，但只要能对话仍可接入（「能否对话」与「是否流式」解耦）。

---

## 4. 字段规范（容易踩坑）

### 4.1 `finish_reason` 白名单（MUST）

出口处**只允许**这 5 个值，传其它值会导致标准客户端解析失败：

| 值 | 含义 |
|---|---|
| `stop` | 正常结束 / 命中停止序列（**异常也兜底归一为它**） |
| `length` | 触达 token 上限 |
| `tool_calls` | 发起工具调用 |
| `content_filter` | 内容安全拦截 |
| `function_call` | （已废弃，新实现不要用） |

- **不存在 `error` 值**：你的服务出错时，要么在未产出内容前返回 HTTP 非 2xx，要么在流式中发一个 `finish_reason:"stop"` 的 stop 帧并附 `error` 字段（见 §5.3），绝不要把 `error` 塞进 `finish_reason`。

### 4.2 `usage`（MUST）

统一字段：`prompt_tokens` / `completion_tokens` / `total_tokens`。无法统计时填 `0`。流式放在 stop 帧。

### 4.3 角色

至少支持 `system` / `user` / `assistant`。不支持 `tool` 角色时可忽略（网关侧会处理）。

---

## 5. 进阶能力（可选，L1 / L2）

不做也能接入；做了能在广场获得更好体验。

### 5.1 思考过程（L1 `reasoning`，建议做）

把"思考中"内容放进 `delta.reasoning`，广场会渲染成"思考中"动画：
```jsonc
data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning":"正在检索…"},"finish_reason":null}]}
```
- 入口也兼容 `reasoning_content` 字段、或正文里的 `<think>…</think>` 标签。
- 注意：reasoning **只出不入**，不要要求把它回传到下一轮 messages。

### 5.2 图片输入（vision）

若支持图片，接受 OpenAI 多模态 `content` 数组：
```jsonc
{ "role":"user", "content":[
  { "type":"text", "text":"这是什么" },
  { "type":"image_url", "image_url": { "url":"https://... | data:image/...;base64,..." } }
]}
```
> 探测不会主动发图，vision 能力默认按 `inferred` 处理（以实测为准）。

### 5.3 流式中途出错

HTTP 头已发出后出错：发一个 `finish_reason:"stop"` 的 stop 帧 + `error` 字段，再 `[DONE]`：
```jsonc
data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"error":{"type":"upstream_error","message":"..."}}

data: [DONE]
```
未产出任何内容就失败 → 直接返回 HTTP `5xx`，不要发半截 SSE。

---

## 6. 鉴权方式（任选其一）

接入向导的 `authType` 决定网关如何带凭证，你的服务按其一校验即可：

| authType | 网关发送的请求头 | 你的服务校验 |
|---|---|---|
| `bearer`（默认，推荐） | `Authorization: Bearer <credential>` | 取 Bearer token |
| `x-api-key` | `x-api-key: <credential>` | 取该头 |
| `custom-header` | `<你指定的头名>: <credential>` | 取该头 |

无效凭证一律返回 `401`。

---

## 7. 接入流程与「探测」通过标准

广场接入是 4 步向导：**平台信息 → 测试验证 → 完善信息 → 审核上线**。第 2 步会对你的服务做一次只读探测，**这是接入的关卡**。

### 7.1 探测的 4 项检查（你的服务要让它们全绿）

| # | 检查 | 你的服务要满足 | 是否决定放行 |
|---|---|---|---|
| 1 | connectivity | `GET {baseUrl}/models` 能返回**任意 HTTP 响应**（别让它 DNS 失败 / 连接超时） | ✅ 是 |
| 2 | credential | 凭证有效时**非 401/403** | ✅ 是 |
| 3 | minimalChat | `POST /chat/completions`（`stream:true, max_tokens:1`）返回 **2xx 且响应可解析为 OpenAI 结构** | ✅ 是 |
| 4 | responseFormat | 响应含 `choices[].delta`（流式优先）或 `choices[].message` | ⬜ 否（仅提示） |

放行公式：**`connected = connectivity && credential && minimalChat`**。
能力判定：`streaming` 实测（收到 SSE chunk = verified）；`vision`/`tools` 推断（inferred，以实测为准）。

### 7.2 试聊（第 2 步可选）

向导会用你填的临时连接信息直连你的服务发起真实对话（不落库、不计统计）。确保你的 `stream:true` 流式正常、逐字返回，体验最佳。

### 7.3 注册 → 审核 → 上架

探测通过后，完善展示信息（名称/头像/简介/分类）提交，经机审 + 人审通过后上架公共广场。凭证由平台**加密托管**，你的真实密钥不会下发给终端用户。

### 7.4 向导「高级配置」字段对照

第 1 步「平台信息」里有一块**高级配置（选填）**，默认值适用于绝大多数 OpenAI 兼容服务，多数情况下**不用动**。各项与你服务的对应关系如下：

| 高级配置项 | 默认值 | 对应你的服务 | 说明 |
|---|---|---|---|
| 鉴权方式 | Bearer Token | §6 的校验逻辑 | 决定网关带凭证的请求头：`bearer` / `x-api-key` / `custom-header` |
| 流式终止符 | `[DONE]` | 你的 SSE 结束哨兵 | 默认 `data: [DONE]`；若你的服务用别的终止符，在此声明，网关据此判定流结束 |
| usage 位置 | stop 帧内 | 你把 `usage` 放哪 | 默认按「合并在 stop 帧」解析；建议你就放 stop 帧（见 §3.2 / §4.2），可省心 |
| 能力声明 | 流式 / 视觉 / 工具 | 你支持哪些能力 | 仅用于展示与默认值；**最终以第 2 步探测/实测为准**，声明与实测不符时以实测覆盖 |

> 说明：「标准协议接入」在后端统一按 **OpenAI 兼容端点** 处理。把服务做成本指南描述的 OpenAI 兼容形态即可，无需任何额外的协议声明。

---

## 8. 自测 checklist + curl

接入前用 curl 自测，全绿再去填向导（把 `BASE`/`KEY` 换成你的）：

```bash
BASE="https://your.host/v1"
KEY="sk-your-key"

# 1) 连通性 + 凭证（期望 200，凭证错误应为 401）
curl -i "$BASE/models" -H "Authorization: Bearer $KEY"

# 2) 非流式最小对话（期望 JSON 含 choices[0].message.content）
curl -s -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"

# 3) 流式（期望多帧 data: ... 且以 data: [DONE] 结尾）
curl -N -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"stream\":true,\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

自测清单：
- [ ] `GET /models` 返回 200；错误凭证返回 401
- [ ] 非流式响应含 `choices[0].message.content` 与 `usage`
- [ ] 流式：首帧 role、中间 content 增量、stop 帧 `finish_reason:"stop"`、结尾 `data: [DONE]`
- [ ] `finish_reason` 只用 5 个白名单值
- [ ] `stream` 严格按布尔解析
- [ ]（可选）`delta.reasoning` 思考过程

---

## 9. 用内置 mock 先跑通向导

如果你还没准备好真实服务，可以先用平台内置的 OpenAI 兼容 mock 上游跑通整套向导，验证前端探测、试聊、失败态展示是否正常。

### 9.1 第 1 步「平台信息」怎么填

| 字段 | 示例值 |
|---|---|
| 智能体平台 | 标准协议接入 |
| API 地址 | `http://<admin_host>:8088/mock/agent/v1` |
| API 密钥 | 任意非空字符串，如 `sk-mock-123` |
| 鉴权方式 | Bearer Token |

提交到第 2 步后，正常场景应看到：
- 连通性检测、凭证校验、发起最小对话、校验响应格式均为通过。
- 能力标签里「流式输出」为通过。
- 试聊框可以发送「你好」，mock 会返回类似 `你说了：「你好」。我是 mock 智能体，这是模拟回复。`

### 9.2 常用失败场景

需要验证失败态时，在 API 地址后加 `?scene=<名字>`：

| API 地址后缀 | 表现 | 用途 |
|---|---|---|
| 不加 | 4 项全绿，支持流式 | 正常接入 |
| `?scene=noauth` | 凭证校验红叉 | 验证密钥错误提示 |
| `?scene=nomodels` | `/models` 返回 404，但可继续用最小对话兜底 | 验证未实现 `/models` 的场景 |
| `?scene=nostream` | 流式能力不通过，返回整段 JSON | 验证非流式服务体验 |
| `?scene=garbage` | 响应格式校验红叉 | 验证非 OpenAI 响应结构 |
| `?scene=error` | 最小对话红叉 | 验证上游服务错误 |

> mock 服务仅用于联调，生产环境不要开启。

---

## 10. 最小参考实现（Python / FastAPI，可直接抄）

一个满足 L0 + 思考过程的最小服务，跑起来即可被接入：

```python
# pip install fastapi uvicorn
# uvicorn app:app --host 0.0.0.0 --port 8000
import json, time
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
VALID_KEY = "sk-your-key"          # 你的密钥

def check_auth(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing credential")
    if authorization[len("Bearer "):] != VALID_KEY:
        raise HTTPException(status_code=401, detail="invalid credential")

@app.get("/v1/models")
def models(authorization: str | None = Header(None)):
    check_auth(authorization)
    return {"object": "list", "data": [{"id": "default", "object": "model", "owned_by": "you"}]}

@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(None)):
    check_auth(authorization)
    body = await request.json()
    stream = bool(body.get("stream", False))          # 严格布尔
    user_msg = next((m["content"] for m in reversed(body.get("messages", []))
                     if m.get("role") == "user"), "")
    answer = f"你说了：「{user_msg}」"                  # 换成你的真实推理
    cid, created = f"chatcmpl-{int(time.time()*1000)}", int(time.time())

    if not stream:
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(user_msg), "completion_tokens": len(answer),
                      "total_tokens": len(user_msg) + len(answer)},
        })

    def sse():
        def frame(delta, finish=None, usage=None):
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "choices": [choice]}
            if usage: chunk["usage"] = usage
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield frame({"role": "assistant"})                       # role 帧
        yield frame({"reasoning": "正在思考…"})                  # 可选 L1 思考
        for ch in answer:                                        # content 增量
            yield frame({"content": ch})
        yield frame({}, finish="stop", usage={                   # stop 帧 + usage
            "prompt_tokens": len(user_msg), "completion_tokens": len(answer),
            "total_tokens": len(user_msg) + len(answer)})
        yield "data: [DONE]\n\n"                                 # 终止哨兵

    return StreamingResponse(sse(), media_type="text/event-stream")
```

部署后 `baseUrl` 填 `http://<host>:8000/v1`，凭证填 `sk-your-key`。

---

## 11. 常见探测失败与排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 连通性红叉 | DNS / 连接失败 / 超时（>5s） | 检查 baseUrl 可公网访问、版本段是否填到 `/v1`、5s 内响应 |
| 凭证红叉（401/403） | 鉴权头取错、authType 不匹配 | 对齐 §6；确认凭证正确 |
| 最小对话红叉 | `/chat/completions` 非 2xx，或响应取不到 `choices[].delta/message` | 对齐 §3 响应结构；能接受 `max_tokens:1` |
| 格式校验红叉 | 响应不是 OpenAI 结构（缺 `choices`） | 按 §3.1/§3.2 输出 |
| 流式能力✗ | 忽略了 `stream:true`，返回整段 JSON | 实现 §3.2 SSE；不做也能接入但无流式体验 |
| 整体超时 | 单项 >5s 或整体 >15s | 优化响应延迟 |

---

## 附：与协议规范的对应

| 本指南 | compat-protocol-spec.md |
|---|---|
| §1 最小契约 / §3 端点 | §2 端点总览、§5 请求 Schema、§6 响应 Schema |
| §3.2 流式帧 | §7 流式 Schema |
| §4.1 finish_reason | §6.1 |
| §4.2 usage | §6.2 / §7.4 |
| §5 进阶 L1/L2 | §8 扩展通道 |
| §6 鉴权 | §3 鉴权、§4.3.2 compat profile auth |
| §7 探测标准 | 接入探测接口设计 §5 |

# 清小搭多模态附件 · 对端接口文档

> 版本：v1.0（2026-07-21）
> 读者：对接清小搭的 **agent 服务开发者 / 平台对接方**。
> 范围：**AI 输出文件产物（attachments）**、**音频输入（input_audio）**、**文件输入（file）** 多模态能力的对端字段约定。
> 说明：本文只描述**对端可见的协议字段与约束**，不涉及清小搭内部实现。完整设计见《智能体输出文件产物（attachments）协议设计提案》。

---

## 0. 总览

| 能力 | 方向 | 承载 | 字段 |
|------|------|------|------|
| **文件产物输出** | agent → 用户 | `x_soda.attachments`（L2 扩展字段） | `fileUrl`/`fileName`/`fileType`/`mimeType`（+3 选填） |
| **音频输入** | 用户 → agent | `input_audio` content part | `url` + `format`（清小搭只给 URL，不给 base64） |
| **文件输入** | 用户 → agent | `file` content part | `file.url` 或 `file.file_id` + `filename` |

**一条贯穿全文的硬规则：入参（音频/文件）清小搭一律只给 URL、不给 base64；出参 attachments 也只放 URL。你的服务按 URL 拉取即可，无需实现 base64 解码。** 原因见 §4。

---

## 1. 文件产物输出（attachments）

当你的 agent 产出文件（PDF 调研报告、PPT、Word、Excel、图片、音频等）要回传给用户时，用 **L2 扩展字段 `x_soda.attachments`** 承载。**只传可下载的 URL，不内嵌文件字节**——你的服务先把文件上传到可公网访问的存储拿到 URL，再放进 attachments。

### 1.1 字段结构

```jsonc
{
  "fileUrl":    "https://your.host/files/report.pdf",  // 必填，可直接 GET 下载的 URL
  "fileName":   "调研报告.pdf",                          // 必填，展示 + 下载文件名
  "fileType":   "pdf",                                 // 必填，类型枚举（见 §1.4）
  "mimeType":   "application/pdf",                     // 必填，= HTTP Content-Type
  "fileSize":   240532,                                // 选填，字节数
  "previewUrl": "https://your.host/files/thumb.png",   // 选填，缩略图 URL
  "expiresAt":  "2026-07-22T10:00:00Z"                 // 选填，签名 URL 过期时间（ISO8601）
}
```

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `fileUrl` | ✅ | 前端可直接 GET 下载的地址（公网或签名 URL） |
| `fileName` | ✅ | 文件名 |
| `fileType` | ✅ | 类型枚举（§1.4） |
| `mimeType` | ✅ | MIME 类型（= HTTP `Content-Type`，如 `application/pdf`） |
| `fileSize` | ⬜ | 字节数 |
| `previewUrl` | ⬜ | 缩略图 URL |
| `expiresAt` | ⬜ | 签名 URL 过期时间（ISO8601） |

一条消息可带多个文件（`attachments` 是数组，如 PDF + 配套 xlsx）。

### 1.2 非流式响应（挂响应顶层）

```jsonc
{
  "id": "chatcmpl-xxx", "object": "chat.completion", "created": 1735689600,
  "choices": [{ "index":0, "message":{"role":"assistant","content":"报告已生成，请查收附件。"}, "finish_reason":"stop" }],
  "usage": { "prompt_tokens":12, "completion_tokens":8, "total_tokens":20 },
  "x_soda": {
    "attachments": [
      { "fileUrl":"https://your.host/files/report.pdf", "fileName":"调研报告.pdf",
        "fileType":"pdf", "mimeType":"application/pdf", "fileSize":240532 }
    ]
  }
}
```

### 1.3 流式响应（挂 stop 帧，与 usage 同帧）

```jsonc
data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20},"x_soda":{"attachments":[{"fileUrl":"https://your.host/files/report.pdf","fileName":"调研报告.pdf","fileType":"pdf","mimeType":"application/pdf"}]}}

data: [DONE]
```

- 只在**结束时**携带一次（stop 帧 / 非流式响应顶层），不要在流式增量帧里发。

### 1.4 `fileType` 枚举

由 `mimeType` 推导；前端据此选图标/预览组件。精确格式判断读 `mimeType`。

| 分组 | `fileType` | 对应 `mimeType` |
|------|-----------|-----------------|
| 图像 | `image` | `image/*` |
| 音频 | `audio` | `audio/*` |
| 视频 | `video` | `video/*` |
| 文档 | `pdf` | `application/pdf` |
| | `word` | `application/msword`、`...wordprocessingml.document` |
| | `excel` | `application/vnd.ms-excel`、`...spreadsheetml.sheet` |
| | `ppt` | `application/vnd.ms-powerpoint`、`...presentationml.presentation` |
| | `text` | `text/plain`、`text/markdown` 等 |
| 压缩包 | `archive` | `application/zip`、`x-rar`、`x-7z-compressed` 等 |
| 兜底 | `file` | 其它一切 |

### 1.5 兼容性

`x_soda` 是 L2 扩展字段。标准 OpenAI 客户端不认识会**安全忽略**，不影响对话主线；清小搭前端会解析并渲染成可下载的文件卡片。不产出文件的 agent 无需关心本节。

### 1.6 文件由小搭统一转存（你无需担心 URL 时效）

你在 `fileUrl` 里给一个**当次可下载的地址即可**——哪怕是你自己的临时签名 URL 也行。清小搭收到后会**把文件转存到自己的对象存储**，再落库和展示。因此：

- 你**不必**保证 `fileUrl` 长期有效，只需保证**清小搭能在响应返回后的短时间内拉取到**。
- 你**不必**自己搭长期文件托管——小搭负责持久化，历史消息里的文件由小搭 OSS 提供。
- 建议仍**优先给 URL**（而非 base64）：base64 会先经过你的响应体和小搭链路，大文件更慢更重。

---

## 2. 音频输入（input_audio）

用户上传/录制的音频（转写、语音问答等）以 OpenAI 兼容的 **`input_audio` content part** 传入。**清小搭只通过 `url` 传入（OSS 公网地址），不给 base64**——你的服务按 URL 拉取即可。

### 2.1 字段结构

```jsonc
{ "role":"user", "content":[
  { "type":"text", "text":"帮我转写这段录音" },
  { "type":"input_audio", "input_audio": { "url":"https://oss.xiaoda.../voice.mp3", "format":"mp3" } }
]}
```

| 字段 | 说明 |
|------|------|
| `input_audio.url` | 音频可下载 URL（清小搭 OSS 公网地址） |
| `input_audio.format` | 音频格式，见 §2.2 |

> 注：OpenAI 原生 `input_audio` 支持 `data`(base64)，但清小搭入参一律用 URL、不下发 base64，你无需处理 base64 分支。

### 2.2 格式

`wav` / `mp3` / `m4a` / `webm`——均为 OpenAI 官方支持格式（OpenAI 完整支持 `mp3`/`mp4`/`mpeg`/`mpga`/`m4a`/`wav`/`webm`）。**原样透传不转码**，能否处理取决于你服务背后的模型；不支持可忽略该 part。

### 2.3 大小限制

音频走 URL，沿用平台全局上限（当前 200MB）。不做时长限制。

### 2.4 能力与探测

- 探测**不主动发音频**，audio 能力按 `inferred` 处理，以实测为准。
- 不支持音频的服务：清小搭会剥离音频 part 并告警，不影响文本对话。

---

## 3. 文件输入（file，pdf/word 等）

用户上传的文档（PDF/Word/Excel/PPT/txt/markdown 等）以 `file` content part 传入。**承载方式同图片/音频：优先 URL**。

### 3.1 字段结构

```jsonc
{ "role":"user", "content":[
  { "type":"text", "text":"总结这份文档" },
  { "type":"file", "file": { "url":"https://.../doc.pdf", "filename":"doc.pdf" } }
  // 或用平台上传接口拿到的 file_id：
  // { "type":"file", "file": { "file_id":"...", "filename":"doc.pdf" } }
]}
```

| 字段 | 说明 |
|------|------|
| `file.url` | 文档可下载 URL（与 `file_id` 二选一，**推荐**） |
| `file.file_id` | 平台上传接口返回的文件 ID（与 `url` 二选一） |
| `file.filename` | 文件名 |

### 3.2 格式与约束

- **格式**：pdf / word(doc/docx) / excel(xls/xlsx) / ppt(pptx) / txt / markdown 等，**原样透传不解析**——能否读取取决于你服务背后的模型/知识库能力。
- **大小**：文件走 URL，沿用全局上限（当前 200MB）。
- **能力与探测**：探测不主动发文件，file 能力按 `inferred` 处理；不支持则剥离 + 告警。

---

## 4. 为什么只用 URL、不用 base64

清小搭会把对话消息（含用户上传的文件、AI 产出的附件）持久化。**若用 base64 承载，这段 base64 会跟随消息落库**，导致单行数据几十 MB、拖垮存储与历史查询。因此：

- **入参（用户 → agent）**：清小搭前端先把文件上传到 OSS，**只把公网 URL 下发给你**，不给 base64。你按 URL 拉取即可，无需实现 base64 解码。
- **出参（agent → 用户）**：你在 `fileUrl` 放 URL 即可（临时签名 URL 也行，清小搭会转存，见 §1.6）；不要内嵌 base64。
- 唯一的 base64 例外：清小搭网关对接「只收 base64 的上游模型」时，在**请求出口临时生成 base64、用完即弃、不落库**——平台内部行为，与你作为对接方无关。

---

## 附：字段速查

**输出 attachments**（4 必填）：`fileUrl` · `fileName` · `fileType` · `mimeType` ｜ 选填：`fileSize` · `previewUrl` · `expiresAt`

**音频输入**：`input_audio.url` + `format(wav/mp3/m4a/webm)`

**文件输入**：`file.url`（或 `file.file_id`）+ `filename`

**铁律**：入参出参都只用 URL，不用 base64。
