# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

将**小米 MiMo AI Studio** 网页端对话转换为 **OpenAI 兼容 API**，支持多模态（文本 + 图片 + 文件）、多账号负载均衡。**本分支不含工具调用逻辑，专注纯对话，输出质量更高。**


本项目基于原[mimo2api](https://github.com/Water008/MiMo2API) 修改。
本项目所修改代码均为ai完成，不含任何一句人工代码，望周知！

> ⚠️ **这是 `no-tools` 分支** — 不支持 Function Calling。如需工具调用，请切换到 [`main` 分支](https://github.com/Fly143/MiMo2API)。


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
  - [深度思考模式](#7-深度思考模式)
  - [模型发现与刷新](#7-模型发现与刷新)
  - [语音合成 (TTS)](#8-语音合成-tts)
- [管理命令](#管理命令)
- [项目结构](#项目结构)
- [配置参考](#配置参考)
- [依赖](#依赖)
- [限制与已知问题](#限制与已知问题)
- [常见问题](#常见问题)
- [为什么选 no-tools](#为什么选-no-tools)
- [许可](#许可)

## 特性

- **OpenAI 完全兼容** — 标准 `/v1/chat/completions`（流式/非流式）、`/v1/models`、`/v1/models/{id}` 端点，可直接对接 ChatBox、NextChat、LobeChat 等任何 OpenAI 客户端
- **多模态支持** — omni 模型支持图片输入（URL、base64），自动完成三步上传流程（genUploadInfo → PUT → resource/parse）；所有模型支持文本文件上传（.md / .txt 等），同样走 MiMo 原生上传流程
- **深度思考** — 支持 reasoning_effort 参数，自动分离 `<think>` 块输出
- **多账号池** — 管理面板配置多个 MiMo 账号，轮询负载均衡，自动故障转移
- **动态模型发现** — 启动时从 MiMo 官方 API 实时拉取可用模型列表，无需手动维护
- **凭证管理** — 支持 Cookie 导入、cURL 导入两种配置方式
- **语音合成（TTS）** — 标准 `/v1/audio/speech` 端点，支持三种模式：内置音色（任意 `mimo-v*.5-tts` 模型）、音色设计（`-voicedesign` 后缀）、语音克隆（`-voiceclone` 后缀 + 音频 data URI）
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
│  ┌─────────┐  ┌──────────────────────┐                   │
│  │ routes  │  │     mimo_client      │                   │
│  │ (API)   │──│ (HTTP/SSE 代理)       │                   │
│  └─────────┘  └──────────────────────┘                   │
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
git clone -b no-tools https://github.com/Fly143/MiMo2API.git
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
  -H "Authorization: Bearer ***
```

返回模型列表会显示所有 MiMo 官方当前可用的模型。

### 2. 文本对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer *** \
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
  -H "Authorization: Bearer *** \
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
  -H "Authorization: Bearer *** \
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
  -H "Authorization: Bearer *** \
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

### 5. 文件上传（文本文件）

支持上传文本文件（`.md`、`.txt` 等），MiMo 会读取文件内容并基于内容回答：

```bash
# 先读取文件并转为 base64
BASE64=$(base64 -w0 yourfile.md)

curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"mimo-v2-pro\",
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

### 6. 深度思考模式

使用 `reasoning_effort` 参数启用深度思考：

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer *** \
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
  -H "Authorization: Bearer ***"
```

### 8. 语音合成 (TTS)

支持 OpenAI 兼容的 `/v1/audio/speech` 端点，三种模式通过**模型名后缀**区分：

#### 8.1 内置音色（默认模式）

使用任何 TTS 模型名即可（如 `mimo-v2.5-tts`），通过 `voice` 参数选择音色：

```bash
curl http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts",
    "input": "今天天气真不错",
    "voice": "冰糖"
  }' --output output.wav
```

支持的语气/音色控制：
- **内置音色：** `冰糖`、`茉莉`、`苏打`、`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`、`mimo_default`
- **OpenAI 兼容名：** `alloy`/`echo`/`fable`/`onyx`/`nova`/`shimmer` 均映射到 `冰糖`
- **语速控制：** `speed` 参数（0.5~2.0）
- **标签控制：** 在 `input` 文本中直接插入 `(河南话)`、`(四川话)`、`[微笑]`、`[生气]` 等标签
- **自然语言风格：** `style` 参数，如 `轻声细语`、`激昂慷慨`

#### 8.2 音色设计（自定义音色）

模型名以 `-voicedesign` 结尾，通过 `style` 参数描述想要的音色：

```bash
curl http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voicedesign",
    "input": "欢迎使用我们的产品",
    "style": "年轻女性，声音甜美，语速适中"
  }' --output output.wav
```

`style` 支持自然语言描述，不传则默认 `生成一个自然流畅的声音`。

#### 8.3 语音克隆（音频样本克隆）

模型名以 `-voiceclone` 结尾，`voice` 参数传音频 data URI：

```bash
# 准备音频样本
BASE64=$(base64 -w0 sample.wav)

curl http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"mimo-v2.5-tts-voiceclone\",
    \"input\": \"这是克隆出来的声音\",
    \"voice\": \"data:audio/wav;base64,${BASE64}\"
  }" --output cloned.wav
```

> **原理：** 自动将音频上传到小米 FDS 文件服务，然后以 FDS URL 作为音色参考提交 TTS 任务。

#### 8.4 客户端配置

**ChatBox / NextChat / LobeChat：**
- 在 TTS 配置中选择 `/v1/audio/speech` 端点（默认 OpenAI 标准路径即可）
- 模型填写 `mimo-v2.5-tts`（内置音色）或其他后缀变体

**RikkaHub：** 直接在聊天中使用标签控制发音（`(河南话)今天可冷`），无需额外配置。

> **📖 参考官方文档：** [小米 MiMo 语音合成 API](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis) / [v2.5 版 TTS](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5) — 包含支持的音色列表、标签控制规则、SSML 等详细信息。

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
- Pydantic v1

```bash
pip install -r requirements.txt
```

## 限制与已知问题

| 限制 | 说明 |
|------|------|
| Token 有效期 | serviceToken 约 24 小时过期，过期后需网页端退出并重新登录（仅刷新 Cookie 无效），见下方 FAQ |
| 多模态模型 | `mimo-v2.5` / `mimo-v2-omni` 支持识图；全系模型支持文件上传与图片 OCR 文字提取 |
| TTS 模型 | 支持 `mimo-v*.5-tts`（内置音色）、`-voicedesign`（音色设计）、`-voiceclone`（语音克隆）三种模式 |
| 并发限制 | 取决于 MiMo 服务端限制（通常 1-2 并发/账号），多账号可缓解 |
| 不支持 Embeddings | 仅实现 Chat Completions 端点 |
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

**Q: tool_call 没有被提取？**
A: 查看日志确认响应内容。如果 MiMo 没有按预期输出工具调用格式，可能是提示词不够清晰，或者该模型理解力有限。推荐使用 `mimo-v2-pro` 进行工具调用。

**Q: 可以部署到公网吗？**
A: 可以，但注意修改默认 API Key（`sk-mimo` 太简单），建议使用 Nginx 反向代理 + HTTPS。

### 为什么选 no-tools

本分支移除了所有工具调用逻辑（`tool_call.py`），**不注入任何工具 prompt**。

效果：
- 上下文更干净 — 模型注意力 100% 在用户问题上
- 输出质量更高 — 不会因格式指令"分心"
- 代码更简洁 — 少一个模块，部署更快
- 不会幻觉 TOOL_CALL — 模型不会无中生有输出工具调用格式

如果后续需要工具调用，切回 `main` 分支即可。

## 许可

MIT License

---

**致谢：** 小米 MiMo AI Studio 提供的基础 API 服务。
[GoblinHonest/mimo2api_mimoapi](https://github.com/GoblinHonest/mimo2api_mimoapi) — 会话管理（消息指纹续接 MiMo conversationId）设计参考。
