# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

将**小米 MiMo AI Studio** 网页端对话转换为 **OpenAI 兼容 API**，支持多模态（文本 + 图片 + 文件）、工具调用（Function Calling）、Anthropic Messages API、多账号负载均衡。


本项目基于原[mimo2api](https://github.com/Water008/MiMo2API) 修改。
本项目所修改代码均为ai完成，不含任何一句人工代码，望周知！

> 📖 [English Version](README_EN.md)

> **💡 TTS 语音合成和 ASR 语音识别已合并到主分支！** 使用 `main` 分支即可获得完整功能。



## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
  - [一键部署](#一键部署)
  - [手动安装](#手动安装)
- [配置凭证](#配置凭证)
  - [方法1：Cookie 导入](#方法1cookie-导入)
  - [方法2：cURL 导入](#方法2curl-导入)
  - [多账号管理](#多账号管理)
- [API 使用](#api-使用)
  - [列出模型](#1-列出模型)
  - [文本对话](#2-文本对话)
  - [流式对话](#3-流式对话)
  - [多模态（图片理解）](#4-多模态图片理解)
  - [文件上传](#5-文件上传文本文件)
  - [工具调用（Function Calling）](#6-工具调用function-calling)
  - [深度思考模式](#7-深度思考模式)
  - [模型发现与刷新](#8-模型发现与刷新)
- [Anthropic Messages API](#9-anthropic-messages-api)
- [TTS 语音合成](#tts-语音合成)
- [ASR 语音识别](#asr-语音识别)
- [Responses API 详解](#responses-api-详解)
- [工具调用详解](#工具调用详解)
- [管理命令](#管理命令)
- [项目结构](#项目结构)
- [配置参考](#配置参考)
- [依赖](#依赖)
- [限制与已知问题](#限制与已知问题)
- [常见问题](#常见问题)
- [许可](#许可)

## 特性

- **OpenAI 完全兼容** — 标准 `/v1/chat/completions`（流式/非流式）、`/v1/models`、`/v1/models/{id}` 端点，可直接对接 ChatBox、NextChat、LobeChat 等任何 OpenAI 客户端
- **Anthropic Messages API 兼容** — 完整支持 `/v1/messages`（流式/非流式）+ count_tokens + batches CRUD + message_get，共 9 个 Anthropic 端点，可对接 RikkaHub 等 Anthropic 客户端
- **工具调用（Function Calling）** — 7 种提取策略覆盖 MiMoML（`<|MiMoML|tool_calls>`）、MiMo 原生 XML (`<tool_call>`)、TOOL_CALL 标签、JSON、`<function_call>` XML、中文格式、自由文本匹配，自动清洗响应中的工具残留
- **流式筛分** — 有工具调用时实时分离正文与工具调用内容，客户端无需等待完整响应即可逐步接收，RikkaHub 等不再全文缓冲
- **多模态支持** — omni 模型支持图片输入（URL、base64），自动完成三步上传流程（genUploadInfo → PUT → resource/parse）；所有模型支持文本文件上传（.md / .txt 等），同样走 MiMo 原生上传流程
- **深度思考** — 支持 reasoning_effort 参数，自动分离 `<think>` 块输出
- **多账号池** — 管理面板配置多个 MiMo 账号，轮询负载均衡，自动故障转移
- **动态模型发现** — 启动时从 MiMo 官方 API 实时拉取可用模型列表，无需手动维护
- **上下文压缩** — 对话超长时自动压缩中间历史为摘要（保留最近 ~20K tokens），避免直接丢弃旧消息
- **凭证管理** — 支持 Cookie 导入、cURL 导入两种配置方式
- **CORS 全开** — 允许任意来源跨域访问
- **TTS 语音合成** — 兼容 OpenAI `/v1/audio/speech` 接口，支持冰糖/茉莉/白桦/苏打/Mia/Chloe 6 种音色，支持 voicedesign 自定义音色和 voiceclone 声音克隆
- **ASR 语音识别** — 兼容 OpenAI Whisper `/v1/audio/transcriptions` 接口，支持自动语言检测，上传音频即可转文字

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                     OpenAI 兼容客户端                        │
│            (ChatBox / LobeChat / curl / SDK)              │
└───────────────┬──────────────────────────────────────────┘
                │  /v1/chat/completions
                ▼
┌──────────────────────────────────────────────────────────┐
│                     MiMo2API (FastAPI)                      │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ routes  │  │ tool_sieve │  │  tool_call   │  │     mimo_client      │ │
│  │ (API)   │──│ (流式筛分)  │──│ (5策略提取)   │──│ (HTTP/SSE 代理)       │ │
│  │anthropic │  │ anthropic  │  │    batch     │                      │ │
│  │ (路由)   │  │ (格式转换)  │  │ (存储/批处理) │                      │ │
│  └─────────┘  └──────────────┘  └──────────────────────┘ │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ config  │  │    utils     │  │      models           │ │
│  │ (多账号) │  │ (图片上传等)  │  │ (OpenAI 数据模型)     │ │
│  └─────────┘  └──────────────┘  └──────────────────────┘ │
└───────────────┬──────────────────────────────────────────┘
                │  HTTPS (SSE)
                ▼
┌──────────────────────────────────────────────────────────┐
│              MiMo API (aistudio.xiaomimimo.com)           │
│              /open-apis/bot/chat (SSE)                    │
└──────────────────────────────────────────────────────────┘
```

## 快速开始

### 一键部署

```bash
# 直接克隆（推荐）
git clone https://github.com/Fly143/MiMo2API.git
cd MiMo2API
chmod +x deploy.sh
./deploy.sh

```

部署完成后，服务已在 **前台** 启动。见下方[管理命令](#管理命令)了解后台运行等方式。

### Docker 部署

```bash
docker run -d -p 8080:8080 -v $(pwd)/config.json:/app/config.json ghcr.io/fly143/mimo2api:latest
```

或使用 docker-compose：

```yaml
services:
  mimo2api:
    image: ghcr.io/fly143/mimo2api:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.json:/app/config.json
    restart: unless-stopped
```

> 💡 **TTS 语音合成和 ASR 语音识别已合并到主分支！** 使用 `main` 分支即可获得完整功能。

### 手动安装

```bash
# 1. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 创建配置文件
cp config.example.json config.json

# 4. 启动
python main.py
```

启动后访问：**http://localhost:8080**

## 配置凭证

打开管理面板 http://localhost:8080 进行配置。

### 方法1：Cookie 导入

1. 访问 https://aistudio.xiaomimimo.com 并登录
2. 打开 **开发者工具** → **Application** → **Storage → Cookies**
3. 找到以下三个关键 Cookie：
   - `serviceToken` — 服务凭证（最重要）
   - `userId` — 用户 ID（纯数字）
   - `xiaomichatbot_ph` — 会话标识
4. 填入管理面板 → 保存

> **提示：** serviceToken 有效期很短（约 24 小时），过期后需要重新导入。

### 方法2：cURL 导入

1. 登录 aistudio.xiaomimimo.com
2. 打开**开发者工具** → **Network** 面板
3. 发送一条消息，找到 `chat` 请求（SSE 类型）
4. 右键 → **Copy as cURL**
5. 粘贴到管理面板 → 自动解析并保存

### 多账号管理

支持添加**多个账号**，代理会**自动轮询**使用：
- 每个请求从账号池取下一个 → 降低单账号限频风险
- 支持测试连接、删除、替换已有账号
- 同一个 userId 重复导入会自动更新（不重复添加）

## API 使用

### 1. 列出模型

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer sk-mimo"
```

返回模型列表会显示所有 MiMo 官方当前可用的模型。

### 2. 文本对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [
      {"role": "user", "content": "你好，请用中文回复"}
    ]
  }'
```

### 3. 流式对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [
      {"role": "user", "content": "讲个故事"}
    ],
    "stream": true
  }'
```

返回标准 SSE 流（`data: ...\n\n`），以 `data: [DONE]\n\n` 结束。

### 4. 多模态（图片理解）

需要选择 **omni/v2.5** 模型。支持两种图片格式：

**URL 方式：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图片里有什么？"},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }]
  }'
```

**Base64 方式：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
      ]
    }]
  }'
```

> **原理：** 代理会自动完成三步上传流程：`genUploadInfo` 获取签名 URL → `PUT` 上传原始数据 → `resource/parse` 注册解析，然后将 `multiMedias` 参数传入聊天 API。

### 5. 文件上传（文本文件）

支持上传文本文件（`.md`、`.txt` 等），MiMo 会读取文件内容并基于内容回答：

```bash
# 先读取文件并转为 base64
BASE64=$(base64 -w0 yourfile.md)

curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"mimo-v2.5-pro\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [
        {\"type\": \"text\", \"text\": \"总结这个文件\"},
        {\"type\": \"file\", \"file\": {\"filename\": \"yourfile.md\", \"file_data\": \"$BASE64\"}}
      ]
    }]
  }"
```

> **支持的格式：** `.txt`、`.md`、`.py`、`.json`、`.yaml` 等纯文本文件。文件走 MiMo 原生上传流程（`mediaType: "file"`），MiMo 按 token 预算自动读取可用部分。

### 6. 工具调用（Function Calling）

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [
      {"role": "user", "content": "北京今天天气怎么样？"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名称"}
          },
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto"
  }'
```

成功时返回 `finish_reason: "tool_calls"`，`message.tool_calls` 包含结构化的函数调用：

```json
{
  "choices": [{
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123...",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\": \"北京\"}"
        }
      }]
    }
  }]
}
```

### 7. 深度思考模式

使用 `reasoning_effort` 参数启用深度思考：

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [
      {"role": "user", "content": "证明根号2是无理数"}
    ],
    "reasoning_effort": "high",
    "stream": true
  }'
```

流式响应中会包含 `reasoning` 字段（对应 MiMo 的 `<think>` 块），内容与文本分开输出。

### 7. 模型发现与刷新

模型列表**启动时自动探测**，从 `https://aistudio.xiaomimimo.com/open-apis/bot/config` 实时拉取，无需手动配置。
> **⚠️ 幽灵模型说明**：MiMo 网页端未清理历史模型名，动态探测会返回约 16 个模型（含 v2-flash、v2-pro、v2-omni、v2.1 系列等旧名），但**实际能使用的只有最新数字系列**（当前为 `mimo-v2.5`、`mimo-v2.5-pro`，以及 TTS/ASR）。旧模型名可能调用失败或返回空回复，属正常现象。

```bash
# 强制刷新模型列表
curl -X POST http://localhost:8080/v1/models/refresh \
  -H "Authorization: Bearer sk-mimo"
```

### 8. Responses API

OpenAI 最新 Responses API 格式，`/v1/responses` 端点：

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

支持流式（`"stream": true`）、工具调用、深度思考、系统指令等，详见下方 [Responses API 详解](#responses-api-详解)。

## 9. Anthropic Messages API

MiMo2API v2.0.0 新增 Anthropic Messages API 完整兼容支持。只需将 API 地址和密钥换过来即可：

```bash
# 非流式对话
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'

# 流式对话
curl -N -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "stream": true,
    "messages": [
      {"role": "user", "content": "讲个故事"}
    ]
  }'
```

### 支持的端点（9 个）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | 发消息（流式/非流式，含思考链） |
| `/v1/messages/count_tokens` | POST | 计算 token 数（本地估算，需 tiktoken） |
| `/v1/messages/{message_id}` | GET | 查询已存储的消息 |
| `/v1/messages/batches` | POST | 创建批量任务 |
| `/v1/messages/batches` | GET | 批量任务列表 |
| `/v1/messages/batches/{batch_id}` | GET | 批量任务详情 |
| `/v1/messages/batches/{batch_id}/cancel` | POST | 取消批量任务 |
| `/v1/messages/batches/{batch_id}/results` | GET | 下载结果 JSONL |
| `/v1/messages/batches/{batch_id}` | DELETE | 删除批量任务 |

### Anthropic 模型名映射

Claude Code CLI 等工具期望 Anthropic 风格的模型名，无法直接使用 `mimo-*` 原生名。本代理在 Anthropic 端点内部自动映射：

| Claude 模型名 | → MiMo 内部模型 |
|---|---|
| `claude-opus-4-6` | `mimo-v2.5-pro` |
| `claude-sonnet-4-6` | `mimo-v2.5` |
| `claude-haiku-4-5` | `mimo-v2.5` |
| `claude-sonnet-4-5` | `mimo-v2.5` |
| `claude-opus-4-1` | `mimo-v2.5-pro` |
| `claude-opus-4-0` | `mimo-v2.5-pro` |
| `claude-sonnet-4-0` | `mimo-v2.5` |
| `claude-3-7-sonnet` | `mimo-v2.5` |
| `claude-3-5-sonnet` | `mimo-v2.5` |
| `claude-3-opus` | `mimo-v2.5-pro` |
| `claude-3-sonnet` | `mimo-v2.5` |
| `claude-3-haiku` | `mimo-v2.5` |

> ⚠️ **重要提醒：** 建议选择 **2.5 系列模型**（`mimo-v2.5-pro`、`mimo-v2.5`、`mimo-v2.5-tts`、`mimo-v2.5-asr` 等）。MiMo 网页版已只提供 2.5 系列，使用其他旧版模型可能导致账号被封禁。

也支持 search/nothinking 变体。MiMo 原生名（`mimo-*`）继续直接使用，`/v1/models` 返回不变，不影响其他软件。

### 认证

Anthropic 客户端使用 `x-api-key` 头（RikkaHub 自动切换），也兼容 `Authorization: Bearer`：

```bash
# x-api-key（Anthropic 原生）
curl -H "x-api-key: sk-mimo" ...

# Authorization Bearer（向后兼容）
curl -H "Authorization: Bearer sk-mimo" ...
```

### 思考链

MiMo 的 `<think>` 标签内容自动转换为 Anthropic thinking block。流式响应按 **thinking → text → tool_use** 顺序输出 content blocks：

```
message_start
  content_block_start (thinking)
    content_block_delta (thinking_delta ×N)
  content_block_stop
  content_block_start (text)
    content_block_delta (text_delta ×N)
  content_block_stop
message_delta + message_stop
```

### 工具调用

支持 Anthropic 格式的工具定义（`input_schema` → OpenAI `parameters` 自动转换）：

```bash
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "现在几点"}
    ],
    "tools": [{
      "name": "get_time",
      "description": "获取当前时间",
      "input_schema": {"type": "object", "properties": {}}
    }]
  }'
```

返回 Anthropic 格式的 `tool_use` blocks：

```json
{
  "content": [
    {"type": "tool_use", "id": "tu_xxx", "name": "get_time", "input": {}}
  ],
  "stop_reason": "tool_use"
}
```

> **注意：** MiMo 的工具调用基于文本 TOOL_CALL 格式模拟，非原生 function calling。

## TTS 语音合成

端点：`POST /v1/audio/speech`（OpenAI 兼容）

语音合成（文本转语音）支持将输入的文本自动转换为自然流畅的语音输出。支持预置音色、音色设计、声音克隆等多种模式。

> 📖 [官方文档](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)

### 支持的模型

| Model ID | 功能 | 音色 | 注意事项 |
|----------|------|------|----------|
| `mimo-v2.5-tts` | 使用预置精品音色进行语音合成 | 预置音色列表 | 支持唱歌模式，不支持音色设计与音色复刻 |
| `mimo-v2.5-tts-voicedesign` | 通过文本描述定制音色 | 文本描述自动生成 | 不支持唱歌模式、预置音色与音色复刻 |
| `mimo-v2.5-tts-voiceclone` | 基于音频样本复刻任意音色 | 音频样本精准复刻 | 不支持唱歌模式、预置音色与音色设计 |

### 基本用法

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts",
    "input": "你好，世界！",
    "voice": "alloy"
  }' \
  --output speech.wav
```

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| model | string | mimo-v2.5-tts | 模型名 |
| input | string | (必需) | 要合成的文本 |
| voice | string | alloy | 音色名（预置音色或 base64 音频） |
| speed | float | 1.0 | 语速 (0.5-2.0) |
| response_format | string | wav | 返回格式 |
| style | string | (空) | voicedesign 模型的音色描述 |

### 预置音色

仅 `mimo-v2.5-tts` 模型支持预置音色。

| OpenAI 音色 | MiMo Voice ID | 语言 | 性别 |
|-------------|---------------|------|------|
| alloy | 冰糖 | 中文 | 女性 |
| echo | 茉莉 | 中文 | 女性 |
| fable | 白桦 | 中文 | 男性 |
| onyx | 苏打 | 中文 | 男性 |
| nova | Mia | 英文 | 女性 |
| shimmer | Chloe | 英文 | 女性 |
| - | Milo | 英文 | 男性 |
| - | Dean | 英文 | 男性 |

> 💡 也可直接使用 MiMo 原生音色 ID（如 `mimo_default`、`冰糖`）作为 `voice` 参数。

### 风格控制

支持通过自然语言或标签控制语音风格：

**自然语言控制：** 在 `style` 参数中描述想要的风格
```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts",
    "input": "今天天气真好啊！",
    "voice": "alloy",
    "style": "用轻快上扬的语调，语速稍快，带着开心的情绪"
  }' \
  --output speech.wav
```

**标签控制：** 在文本开头嵌入风格标签
```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts",
    "input": "(温柔)你好，欢迎来到小米之家。",
    "voice": "alloy"
  }' \
  --output speech.wav
```

**支持的风格标签：**

| 类型 | 示例 |
|------|------|
| 基础情绪 | 开心/悲伤/愤怒/恐惧/惊讶/兴奋/委屈/平静/冷漠 |
| 复合情绪 | 怅然/欣慰/无奈/愧疚/释然/嫉妒/厌倦/忐忑/动情 |
| 整体语调 | 温柔/高冷/活泼/严肃/慵懒/俏皮/深沉/干练/凌厉 |
| 音色定位 | 磁性/醇厚/清亮/空灵/稚嫩/苍老/甜美/沙哑/醇雅 |
| 人设腔调 | 夹子音/御姐音/正太音/大叔音/台湾腔 |
| 方言 | 东北话/四川话/河南话/粤语 |
| 角色扮演 | 孙悟空/林黛玉 |
| 唱歌 | 唱歌/sing/singing |

**唱歌模式：** 在文本最开头添加 `(唱歌)` 标签，格式为：`(唱歌)歌词`

**音频细粒度标签：** 可在文本中任意位置插入 `[音频标签]` 进行精细控制：
- 语速与节奏：`[吸气]`/`[深呼吸]`/`[叹气]`/`[长叹一口气]`
- 情绪状态：`[紧张]`/`[害怕]`/`[激动]`/`[疲惫]`/`[撒娇]`
- 语音特征：`[颤抖]`/`[变调]`/`[破音]`/`[气声]`/`[沙哑]`
- 哭笑表达：`[笑]`/`[轻笑]`/`[大笑]`/`[冷笑]`/`[抽泣]`/`[哽咽]`

### 自定义音色 (voicedesign)

通过文本描述自动生成音色：

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voicedesign",
    "input": "你好，世界！",
    "voice": "alloy",
    "style": "温柔甜美的女声，语速适中"
  }' \
  --output speech.wav
```

### 声音克隆 (voiceclone)

基于音频样本复刻任意音色，`voice` 参数传入参考音频的 base64 编码：

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voiceclone",
    "input": "这是克隆声音后生成的语音。",
    "voice": "data:audio/wav;base64,UklGRi..."
  }' \
  --output speech.wav
```

## ASR 语音识别

端点：`POST /v1/audio/transcriptions`（OpenAI Whisper 兼容）

语音识别支持将输入的音频自动转换为文本输出，适用于会议转写、歌词识别、方言转写、嘈杂环境录音等场景。

> 📖 [官方文档](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)

### 支持的模型

当前仅支持 `mimo-v2.5-asr` 模型。

### 核心能力

- **多语种识别** — 支持中英双语识别及自动语种检测，原生支持粤语、吴语、闽南语、四川话等中国方言
- **高鲁棒性** — 在噪声、远场拾音、多人重叠对话等复杂声学条件下保持稳定识别，支持带伴奏的歌词转写
- **精准识别** — 精准识别古诗词、专业术语、人名地名等知识密集型内容，自动生成标点无需后处理

### 基本用法

```bash
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@audio.mp3" \
  -F "language=auto"
```

返回：

```json
{"text": "识别出的文本内容"}
```

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| file | file | (必需) | 音频文件 (wav/mp3) |
| model | string | mimo-v2.5-asr | 模型名 (可忽略) |
| language | string | auto | 语言代码 (auto/zh/en 等) |
| response_format | string | json | 返回格式: json 或 text |

### 支持的音频格式

目前支持 `wav` 和 `mp3` 格式的音频样本文件，传入前需将音频文件转换为 Base64 编码字符串，Base64 编码后的字符串大小上限为 **10MB**。

| 格式 | MIME 类型 |
|------|----------|
| wav | audio/wav |
| mp3 | audio/mpeg |

### 使用示例

```bash
# 识别中文音频
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@meeting.wav" \
  -F "language=zh"

# 识别英文音频
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@speech.mp3" \
  -F "language=en"

# 自动检测语言
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@audio.wav" \
  -F "language=auto"
```

---

## 工具调用详解

MiMo API 本身**不支持** OpenAI function calling 格式。本代理通过**MiMoML 提示词注入 + 5 策略提取**实现：

### 提示词注入

将 OpenAI tools 定义转换为 MiMoML（MiMo Markup Language）格式，注入到 system 消息中：

```xml
<|MiMoML|tool_calls>
  <|MiMoML|invoke name="get_weather">
    <|MiMoML|parameter name="city"><![CDATA[北京]]></|MiMoML|parameter>
  </|MiMoML|invoke>
</|MiMoML|tool_calls>
```

### 5 种提取策略（按优先级）

| 策略 | 格式 | 说明 |
|------|------|------|
| MiMoML | `<\|MiMoML\|tool_calls><\|MiMoML\|invoke name="X">...</\|MiMoML\|invoke></\|MiMoML\|tool_calls>` | 主力格式，7 种噪声变体容错 |
| TOOL_CALL | `TOOL_CALL: name(key=value)` | 旧格式兜底 |
| JSON | `{"name":"x","arguments":{...}}` | JSON 块解析 |
| XML | `<tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>` | MiMo 原生 XML |
| 混合 | `<function_call>{"name":"x","arguments":{...}}</function_call>` | XML 包裹 JSON |

### 容错能力

- **噪声容错** — 支持缺管道、重复 `<`、全宽 `｜`、连字符 `mimoml-` 等 7 种格式变体
- **围栏代码块** — 自动跳过 markdown 代码块内的 MiMoML 示例
- **JSON 修复** — 未加引号 key、缺失数组括号、非法反斜杠自动修复
- **Schema 归一化** — 根据 tool schema 将非字符串值自动转为字符串
- **CDATA 保护** — content/command/prompt 等文本参数保留原始字符串
- **缺失开标签** — 有关闭标签无开头时自动补回

### 响应清理

提取成功后，自动清理响应中的工具残留文本（MiMoML 标签、XML 标签、TOOL_CALL 行、JSON 块、CDATA）。

### 流式筛分

有工具调用且 `stream: true` 时，`tool_sieve` 引擎逐字扫描 MiMo 响应流，实时分离**正文内容**和**工具调用文本**：

- **正文** → 即时转为 `delta.content` 逐块输出，客户端无需等待即可显示
- **工具调用** → 缓冲至流结束后解析，然后作为 `tool_calls` 一次性输出

非筛分模式（无工具流、非流）不受影响，保持原有逻辑。筛选检测支持三种格式：`TOOL_CALL:`、`<tool_call>`、`<function=`，同时白名单排除 `<think>` 深度思考标签。

## Responses API 详解

端点：`POST /v1/responses`

MiMo2API 完整实现了 OpenAI Responses API 格式，支持与 Chat Completions 相同的底层能力。

### 与 Chat Completions 的区别

| | Chat Completions | Responses API |
|---|---|---|
| 端点 | `/v1/chat/completions` | `/v1/responses` |
| 消息字段 | `messages` | `input` |
| 系统指令 | `messages[role=system]` | `instructions` |
| 工具格式 | `tool.function.name` | `tool.name` |
| 响应格式 | `choices[0].message` | `output[]` 数组 |
| 思考内容 | `reasoning_content` | `output[type=reasoning]` |
| 工具调用 | `message.tool_calls` | `output[type=function_call]` |

### 基本用法

```bash
# 非流式
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "你好"}]
  }'

# 流式（SSE）
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "讲个故事"}],
    "stream": true
  }'
```

### 工具调用

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "现在几点"}],
    "tools": [{
      "type": "function",
      "name": "get_time",
      "description": "获取当前时间",
      "parameters": {
        "type": "object",
        "properties": {
          "timezone": {"type": "string"}
        }
      }
    }]
  }'
```

> **注意工具格式：** Responses API 的 `tools` 没有 `function` 嵌套层，`name` 直接在顶层（不同于 Chat Completions 的 `tool.function.name`）。MiMo2API 兼容两种格式。

### 响应格式

```json
{
  "output": [
    {
      "type": "reasoning",
      "summary": [{"type": "summary_text", "text": "模型思考内容..."}]
    },
    {
      "type": "function_call",
      "id": "fc_abc123...",
      "call_id": "call_xyz789...",
      "name": "get_time",
      "arguments": "{}"
    },
    {
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [{"type": "output_text", "text": "现在是..."}]
    }
  ]
}
```

`output` 按顺序包含：reasoning（如有）→ function_call（如有）→ message。

## 管理命令

```bash
# 前台运行（Ctrl+C 停止）
./venv/bin/python main.py

# 后台运行
nohup ./venv/bin/python main.py > mimo.log 2>&1 &
echo $! > mimo.pid

# 从 PID 文件停止
kill $(cat mimo.pid)

# 按进程名停止
pkill -f "python main.py"

# 查看实时日志
tail -f mimo.log

# 查看进程状态
ps aux | grep "python main.py"

# 查看端口占用
lsof -i :8080
```

**启动后：**

| 地址 | 说明 |
|------|------|
| `http://localhost:8080` | Web 管理后台（配置账号） |
| `http://localhost:8080/v1` | OpenAI + Anthropic 兼容 API 根路径 |
| `http://localhost:8080/docs` | Swagger API 文档 |
| `http://localhost:8080/v1/messages` | Anthropic Messages API |
| `http://localhost:8080/v1/responses` | OpenAI Responses API |

## 项目结构

```
MiMo2API/
├── main.py                  # 入口，FastAPI 应用创建 + uvicorn 启动
├── deploy.sh                # 一键部署脚本（安装依赖、初始化配置）
├── requirements.txt         # Python 依赖
├── config.example.json      # 配置文件模板
├── config.json              # 实际配置（.gitignore，含凭证）
├── app/
    ├── __init__.py
    ├── routes.py            # API 路由（chat/models/管理面板/账号CRUD）
    ├── anthropic_routes.py  # Anthropic Messages API 路由（9 个端点）
    ├── anthropic.py         # Anthropic ↔ OpenAI 格式转换核心
    ├── batch.py             # Anthropic 批量任务 + count_tokens
    ├── models.py            # OpenAI 兼容数据模型（Pydantic）
    ├── mimo_client.py       # MiMo API 客户端（HTTP SSE 流处理）
    ├── config.py            # 配置管理（多账号、线程安全、轮询）
    ├── utils.py             # 工具函数（cURL解析、图片上传、消息构建）
    ├── tool_sieve.py        # 流式筛分引擎（实时分离工具调用与正文）
    ├── tool_call.py         # 工具调用（提示词注入 + 5策略提取 + 清理）
    ├── usage_store.py       # 用量数据持久化
    ├── session_store.py     # 会话管理（指纹续接 conversationId）
    ├── response_store.py    # Responses API 记录持久化
    └── web/
        └── index.html       # Web 管理面板
```

## 配置参考

`config.json` 完整配置项：

```json
{
  "api_keys": "sk-mimo,sk-another",
  "mimo_accounts": [
    {
      "service_token": "eyJ...",
      "user_id": "123456",
      "xiaomichatbot_ph": "abc123...",
      "is_valid": true,
      "login_time": "04-26 17:00",
      "last_test": "04-26 17:05"
    }
  ],
  "models": []
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_keys` | 逗号分隔的 API Key 列表 | `sk-mimo` |
| `mimo_accounts` | MiMo 账号列表（可多个） | `[]` |
| `models` | 自定义模型列表（空数组=自动探测） | `[]` |

**环境变量：** `PORT` — 监听端口（默认 `8080`）

## 依赖

- **Python 3.10+**
- FastAPI 0.115
- uvicorn 0.32
- httpx 0.27
- Pydantic v1

```bash
pip install -r requirements.txt
```

## 限制与已知问题

| 限制 | 说明 |
|------|------|
| Token 有效期 & 静默降级 | serviceToken 约 24 小时过期。过期后基础聊天（2.5 系列）可能仍然正常，但 **mimo-v2.5 / mimo-v2.5 多模态识图**会静默失效。管理面板"测试连接"只检查普通 chat 端点，无法发现此问题。修复需网页端退出并重新登录，见下方 FAQ |
| 多模态模型 | `mimo-v2.5` / `mimo-v2.5` 支持识图；全系模型支持文件上传与图片 OCR 文字提取 |
| 并发限制 | 取决于 MiMo 服务端限制（通常 1-2 并发/账号），多账号可缓解 |
| 不支持 Embeddings | 仅实现 Chat Completions 和 Responses 端点 |
| 非流式实际走 SSE | MiMo API 只提供 SSE 流，非流式请求会缓冲全部 SSE 后合并返回 |

## 常见问题

**Q: 为什么返回 401 "invalid api key"？**
A: 检查 `Authorization` header 是否携带了正确的 API Key。默认是 `sk-mimo`，可在 `config.json` 中修改。

**Q: 为什么返回 503 "no mimo account"？**
A: 管理面板中没有配置账号，或者所有账号都已失效。请登录 http://localhost:8080 添加有效账号。

**Q: 图片上传失败怎么办？模型说"没有看到图片"？**  
A: 通常是因为服务端 session 状态异常，仅重新获取 Cookie 无效。正确步骤：  
1. 浏览器打开 https://aistudio.xiaomimimo.com  
2. **退出登录**（必须退出，不能只刷新页面）  
3. 重新登录  
4. 在管理面板重新导入 Cookie  
如果是账号被限制，换另一个账号。  

**Q: mimo-v2.5 / mimo-v2.5 多模态识图突然失效，但测试连接显示正常？**  
A: 这是 serviceToken 过期后的**静默降级**现象。MiMo API 对多模态识图的凭证校验比普通聊天严格。Token 过期后：  
- 基础聊天（2.5 系列）可能仍能正常使用  
- 管理面板"测试连接"也显示正常（它只检查普通 chat 端点）  
- 但多模态识图会返回胡说八道的结果或报错  

**症状判断：** 如果普通对话正常，但多模态识图突然失效，大概率是凭证过期。  
**修复：** 同上——网页端退出重新登录，再导入新 Cookie。如果换了新 Cookie 仍无效，换另一个账号试试。

**Q: tool_call 没有被提取？**
A: 查看日志确认响应内容。如果 MiMo 没有按预期输出工具调用格式，可能是提示词不够清晰，或者该模型理解力有限。推荐使用 `mimo-v2.5-pro` 进行工具调用。

**Q: 可以部署到公网吗？**
A: 可以，但注意修改默认 API Key（`sk-mimo` 太简单），建议使用 Nginx 反向代理 + HTTPS。

## 许可

MIT License

---

**致谢：**

- 小米 MiMo AI Studio 提供的基础 API 服务。
- [GoblinHonest/mimo2api_mimoapi](https://github.com/GoblinHonest/mimo2api_mimoapi) — 会话管理（消息指纹续接 MiMo conversationId）设计参考。
- [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) — DSML 工具调用格式与流式筛分引擎设计参考。
