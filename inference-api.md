# 推理实例 API 调用指南

llama-server 提供 **OpenAI 兼容 API**，支持文本和图片（视觉）对话。

## 实例信息

| 实例 | 端口 | API 地址 | 模型 | 能力 |
|------|------|----------|------|------|
| 视觉推理 | 11435 | `http://10.0.6.226:11435/v1` | Qwen3-VL 8B Q4_K_M | 文字 + 图片 |
| 文本推理 | 11434 | `http://10.0.6.226:11434/v1` | Qwen3.6 27B / DeepSeek-R1 7B | 文字 |

> 实例需在后台管理页面手动开启，关闭后不可调用。

## 基础参数

| 参数 | 值 | 说明 |
|------|------|------|
| 端点 | `/v1/chat/completions` | OpenAI 兼容 |
| model | 任意值（不严格校验） | 建议用实际模型名 |
| API Key | 不需要 | 未配 `--api-key` |
| 上下文 | 32768 tokens | `--ctx-size 32768` |
| 图片最小 token | 1024 | `--image-min-tokens 1024` |

---

## curl 调用示例

### 1. 纯文本对话

```bash
curl http://10.0.6.226:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl-8b",
    "messages": [
      {"role": "user", "content": "你好，请介绍一下你自己"}
    ]
  }'
```

### 2. 图片对话（base64 内联，最可靠）

```bash
# 图片转 base64
IMAGE_B64=$(base64 -w 0 photo.jpg)

curl http://10.0.6.226:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"qwen3-vl-8b\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [
        {\"type\": \"text\", \"text\": \"描述这张图片的内容\"},
        {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,$IMAGE_B64\"}}
      ]
    }]
  }"
```

### 3. 流式响应（SSE）

```bash
curl http://10.0.6.226:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl-8b",
    "messages": [{"role": "user", "content": "讲个故事"}],
    "stream": true
  }'
```

### 4. 多轮对话

```bash
curl http://10.0.6.226:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl-8b",
    "messages": [
      {"role": "system", "content": "你是一个专业的安全审计员"},
      {"role": "user", "content": "什么是 SQL 注入？"},
      {"role": "assistant", "content": "SQL 注入是..."},
      {"role": "user", "content": "如何防御？"}
    ]
  }'
```

---

## Python 调用示例

### 安装依赖

```bash
pip install openai
```

### 文本 + 图片对话

```python
from openai import OpenAI
import base64

client = OpenAI(
    base_url="http://10.0.6.226:11435/v1",
    api_key="not-needed"  # 未配 key，随便填
)

# ── 纯文本 ──
resp = client.chat.completions.create(
    model="qwen3-vl-8b",
    messages=[{"role": "user", "content": "你好"}]
)
print(resp.choices[0].message.content)

# ── 图片对话 ──
with open("photo.jpg", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="qwen3-vl-8b",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图里有什么？"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]
    }]
)
print(resp.choices[0].message.content)

# ── 流式响应 ──
stream = client.chat.completions.create(
    model="qwen3-vl-8b",
    messages=[{"role": "user", "content": "讲个故事"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

---

## JavaScript / Node.js 调用示例

### 安装依赖

```bash
npm install openai
```

### 文本 + 图片对话

```javascript
import OpenAI from 'openai'
import fs from 'fs'

const client = new OpenAI({
    baseURL: 'http://10.0.6.226:11435/v1',
    apiKey: 'not-needed'
})

// 纯文本
const resp = await client.chat.completions.create({
    model: 'qwen3-vl-8b',
    messages: [{ role: 'user', content: '你好' }]
})
console.log(resp.choices[0].message.content)

// 图片对话
const imgB64 = fs.readFileSync('photo.jpg').toString('base64')
const resp2 = await client.chat.completions.create({
    model: 'qwen3-vl-8b',
    messages: [{
        role: 'user',
        content: [
            { type: 'text', text: '描述这张图片' },
            { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${imgB64}` } }
        ]
    }]
})
console.log(resp2.choices[0].message.content)
```

---

## 其他端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/v1/models` | GET | 获取可用模型列表 |
| `/v1/chat/completions` | POST | 对话（文本/图片/流式） |
| `/metrics` | GET | Prometheus 指标（token 速率、KV 缓存等） |
| `/health` | GET | 健康检查 |

### 获取模型列表

```bash
curl http://10.0.6.226:11435/v1/models
```

### 健康检查

```bash
curl http://10.0.6.226:11435/health
```

---

## 响应格式（OpenAI 标准）

### 非流式

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "qwen3-vl-8b",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "回答内容"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 50,
    "total_tokens": 75
  }
}
```

### 流式（SSE）

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

## 注意事项

1. **图片格式**：用 `data:image/jpeg;base64,...` 内联最可靠。llama-server 对外部 URL 图片支持不稳定
2. **图片 token**：每张图至少占 1024 tokens，上下文 32768 可容纳多张图
3. **无鉴权**：端口 11434/11435 暴露在内网，任何人都能调用。如需限制：
   - 通过 nginx 反向代理加 API key
   - 或在 `api/compute.go` 启动参数加 `--api-key YOUR_KEY`
4. **并发**：`--parallel 1`，同时只处理 1 个请求，多请求会排队（`deferred_requests` 指标可见）
5. **实例需手动开启**：服务重启后推理实例不会自动启动，需在后台管理页面手动开启
   - 视觉：`POST /api/compute/vision/enable`
   - 文本：`POST /api/compute/inference/start`
