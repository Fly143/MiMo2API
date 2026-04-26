# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

将**小米 MiMo AI Studio** 网页端对话转换为 **OpenAI 兼容 API**，支持多模态（文本 + 图片）、工具调用（Function Calling）、多账号负载均衡。
本项目基于原[mimo2api](https://github.com/Water008/MiMo2API) 修改。
本项目所修改代码均为ai完成，不含任何一句人工代码，望周知！

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
  - [工具调用（Function Calling）](#5-工具调用function-calling)
  - [深度思考模式](#6-深度思考模式)
  - [模型发现与刷新](#7-模型发现与刷新)
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
- **工具调用（Function Calling）** — 5 种提取策略覆盖 MiMo 原生 XML (`<tool_call>`)、TOOL_CALL 标签、JSON、`<function_call>` XML、自由文本匹配，自动清洗响应中的工具残留
- **多模态支持** — omni 模型支持图片输入（URL、base64），自动完成三步上传流程（genUploadInfo → PUT → resource/parse）
- **深度思考** — 支持 reasoning_effort 参数，自动分离 `<think>` 块输出
- **多账号池** — 管理面板配置多个 MiMo 账号，轮询负载均衡，自动故障转移
- **动态模型发现** — 启动时从 MiMo 官方 API 实时拉取可用模型列表，无需手动维护
- **凭证管理** — 支持 Cookie 导入、cURL 导入两种配置方式
- **CORS 全开** — 允许任意来源跨域访问

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
│  │ routes  │  │  tool_call   │  │     mimo_client      │ │
│  │ (API)   │──│ (5策略提取)   │──│ (HTTP/SSE 代理)       │ │
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
tar xzf MiMo2API.tar.gz
cd MiMo2API
chmod +x deploy.sh
./deploy.sh
```

部署完成后，服务已在 **前台** 启动。见下方[管理命令](#管理命令)了解后台运行等方式。

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
    "model": "mimo-v2-flash",
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
    "model": "mimo-v2-flash",
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
    "model": "mimo-v2-omni",
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
    "model": "mimo-v2-omni",
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

### 5. 工具调用（Function Calling）

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
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

### 6. 深度思考模式

使用 `reasoning_effort` 参数启用深度思考：

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
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

```bash
# 强制刷新模型列表
curl -X POST http://localhost:8080/v1/models/refresh \
  -H "Authorization: Bearer sk-mimo"
```

## 工具调用详解

MiMo API 本身**不支持** OpenAI function calling 格式。本代理通过**提示词注入 + 多策略提取**实现：

### 提示词注入

将 OpenAI tools 定义转换为极简文本，注入到 system 消息中：

```text
# Tools
- get_weather(city) — 查询指定城市的天气
- search_web(query, page) — 搜索网页
```

### 5 种提取策略（按优先级）

| 策略 | 格式 | 说明 |
|------|------|------|
| 1 | `TOOL_CALL: name(key=value)` | 正则匹配，最可靠 |
| 2 | `{"name": "x", "arguments": {...}}` | JSON 块解析 |
| 3 | `name(args)` | 自由文本关键词匹配 |
| 4 | `<tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>` | MiMo 原生 XML 格式 |
| 5 | `<function_call>{"name":"x","arguments":{...}}</function_call>` | XML 包裹 JSON |

### 响应清理

提取成功后，自动清理响应中的工具残留文本（TOOL_CALL 行、XML 标签、JSON 块），避免 TTS 误读。

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
| `http://localhost:8080/v1` | OpenAI 兼容 API 根路径 |
| `http://localhost:8080/docs` | Swagger API 文档 |

## 项目结构

```
MiMo2API/
├── main.py                  # 入口，FastAPI 应用创建 + uvicorn 启动
├── deploy.sh                # 一键部署脚本（安装依赖、初始化配置）
├── requirements.txt         # Python 依赖
├── config.example.json      # 配置文件模板
├── config.json              # 实际配置（.gitignore，含凭证）
└── app/
    ├── __init__.py
    ├── routes.py            # API 路由（chat/models/管理面板/账号CRUD）
    ├── models.py            # OpenAI 兼容数据模型（Pydantic）
    ├── mimo_client.py       # MiMo API 客户端（HTTP SSE 流处理）
    ├── config.py            # 配置管理（多账号、线程安全、轮询）
    ├── utils.py             # 工具函数（cURL解析、图片上传、消息构建）
    ├── tool_call.py         # 工具调用（提示词注入 + 5策略提取 + 清理）
    └── admin.html           # Web 管理面板（内嵌单文件）
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
- Pydantic v2

```bash
pip install -r requirements.txt
```

## 限制与已知问题

| 限制 | 说明 |
|------|------|
| Token 有效期 | serviceToken 约 24 小时过期，过期后需重新登录 |
| 多模态模型 | 仅 `mimo-v2-omni/mimo-v2.5` 支持图片；自动切换模型会导致请求 model 与响应 model 不一致 |
| TTS 模型 | `mimo-v2-tts` 需要官方 API Key，逆向方式不支持 |
| 并发限制 | 取决于 MiMo 服务端限制（通常 1-2 并发/账号），多账号可缓解 |
| 不支持 Embeddings | 仅实现 Chat Completions 端点 |
| 非流式实际走 SSE | MiMo API 只提供 SSE 流，非流式请求会缓冲全部 SSE 后合并返回 |

## 常见问题

**Q: 为什么返回 401 "invalid api key"？**
A: 检查 `Authorization` header 是否携带了正确的 API Key。默认是 `sk-mimo`，可在 `config.json` 中修改。

**Q: 为什么返回 503 "no mimo account"？**
A: 管理面板中没有配置账号，或者所有账号都已失效。请登录 http://localhost:8080 添加有效账号。

**Q: 图片上传失败怎么办？**
A: 可能是 Cookie 过期导致上传签名获取失败。重新导入 Cookie/login 即可。

**Q: tool_call 没有被提取？**
A: 查看日志确认响应内容。如果 MiMo 没有按预期输出工具调用格式，可能是提示词不够清晰，或者该模型理解力有限。推荐使用 `mimo-v2-pro` 进行工具调用。

**Q: 可以部署到公网吗？**
A: 可以，但注意修改默认 API Key（`sk-mimo` 太简单），建议使用 Nginx 反向代理 + HTTPS。

## 许可


---


**致谢：** 小米 MiMo AI Studio 提供的基础 API 服务。
