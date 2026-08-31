# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

Convert **Xiaomi MiMo AI Studio** web chat into an **OpenAI-compatible API**, with multimodal support (text + images + files), function calling, Anthropic Messages API, and multi-account load balancing.

本项目基于原[mimo2api](https://github.com/Water008/MiMo2API) 修改。
本项目所修改代码均为ai完成，不含任何一句人工代码，望周知！

> 📖 [中文版本](README.md)

> **💡 TTS speech synthesis and ASR speech recognition are now merged into the main branch!** Use the `main` branch for full functionality.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
  - [One-Click Deploy](#one-click-deploy)
  - [Manual Install](#manual-install)
- [Authentication](#authentication)
  - [Method 1: Cookie Import](#method-1-cookie-import)
  - [Method 2: cURL Import](#method-2-curl-import)
  - [Multi-Account Management](#multi-account-management)
- [API Usage](#api-usage)
  - [List Models](#1-list-models)
  - [Text Chat](#2-text-chat)
  - [Streaming Chat](#3-streaming-chat)
  - [Multimodal (Vision)](#4-multimodal-vision)
  - [File Upload (Text Files)](#5-file-upload-text-files)
  - [Function Calling](#6-function-calling)
  - [Deep Thinking](#7-deep-thinking)
  - [Model Discovery & Refresh](#8-model-discovery--refresh)
- [Anthropic Messages API](#9-anthropic-messages-api)
- [TTS Speech Synthesis](#tts-speech-synthesis)
- [ASR Speech Recognition](#asr-speech-recognition)
- [Responses API](#responses-api)
- [Tool Calling Details](#tool-calling-details)
- [Management Commands](#management-commands)
- [Project Structure](#project-structure)
- [Configuration Reference](#configuration-reference)
- [Dependencies](#dependencies)
- [Limitations & Known Issues](#limitations--known-issues)
- [FAQ](#faq)
- [License](#license)

## Features

- **OpenAI Fully Compatible** — Standard `/v1/chat/completions` (streaming/non-streaming), `/v1/models`, `/v1/models/{id}` endpoints, works with any OpenAI client (ChatBox, NextChat, LobeChat, etc.)
- **Anthropic Messages API** — Full support for `/v1/messages` (streaming/non-streaming) + count_tokens + batches CRUD + message_get, 9 Anthropic endpoints total, compatible with RikkaHub and other Anthropic clients
- **Function Calling** — 7 extraction strategies covering MiMoML (`<|MiMoML|tool_calls>`), MiMo native XML (`<tool_call>`), TOOL_CALL tags, JSON, `<function_call>` XML, Chinese format, free-text matching, with automatic response cleanup
- **Streaming Sieve** — Real-time separation of content and tool call data during streaming, clients receive output progressively without buffering the full response
- **Multimodal** — omni models support image input (URL, base64), auto-completing the 3-step upload flow (genUploadInfo → PUT → resource/parse); all models support text file upload (.md / .txt etc.) via MiMo's native upload pipeline
- **Deep Thinking** — Reasoning effort parameter support, automatic `<think>` tag separation in output
- **Multi-Account Pool** — Admin panel for configuring multiple MiMo accounts, round-robin load balancing, automatic failover
- **Dynamic Model Discovery** — Real-time model list fetched from MiMo official API at startup, no manual maintenance
- **Context Compression** — Auto-compress middle history into summary when conversation gets too long (preserves recent ~20K tokens), avoiding direct message dropping
- **Credential Management** — Support for Cookie import and cURL import configuration methods
- **CORS Fully Open** — Cross-origin access from any source
- **TTS Speech Synthesis** — Compatible with OpenAI `/v1/audio/speech` endpoint, supports 8 preset voices, voicedesign custom voice, and voiceclone voice cloning
- **ASR Speech Recognition** — Compatible with OpenAI Whisper `/v1/audio/transcriptions` endpoint, supports Chinese/English and dialect recognition with automatic language detection

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  OpenAI Compatible Client                  │
│            (ChatBox / LobeChat / curl / SDK)              │
└───────────────┬──────────────────────────────────────────┘
                │  /v1/chat/completions
                ▼
┌──────────────────────────────────────────────────────────┐
│                     MiMo2API (FastAPI)                      │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ routes  │  │ tool_sieve │  │  tool_call   │  │     mimo_client      │ │
│  │ (API)   │──│ (streaming  │──│ (5-strategy   │──│ (HTTP/SSE proxy)      │ │
│  │anthropic │  │ sieve)     │  │  extraction) │                      │ │
│  │ (routes) │  │ anthropic  │  │    batch     │                      │ │
│  └─────────┘  │ (fmt conv.) │  │ (storage)    │                      │ │
│               └──────────────┘  └──────────────────────┘ │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ config  │  │    utils     │  │      models           │ │
│  │ (multi-  │  │ (image up-   │  │ (OpenAI data models)  │ │
│  │ account) │  │  load, etc.) │  │                      │ │
│  └─────────┘  └──────────────┘  └──────────────────────┘ │
└───────────────┬──────────────────────────────────────────┘
                │  HTTPS (SSE)
                ▼
┌──────────────────────────────────────────────────────────┐
│              MiMo API (aistudio.xiaomimimo.com)           │
│              /open-apis/bot/chat (SSE)                    │
└──────────────────────────────────────────────────────────┘
```

## Quick Start

### One-Click Deploy

```bash
# Direct clone (recommended)
git clone https://github.com/Fly143/MiMo2API.git
cd MiMo2API
chmod +x deploy.sh
./deploy.sh
```

After deployment, the service starts in **foreground**. See [Management Commands](#management-commands) below for background running.

### Docker

```bash
docker run -d -p 8080:8080 -v $(pwd)/config.json:/app/config.json ghcr.io/fly143/mimo2api:latest
```

Or with docker-compose:

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

> 💡 **TTS and ASR are now in main branch!**

### Manual Install

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create config file
cp config.example.json config.json

# 4. Start
python main.py
```

After startup, visit: **http://localhost:8080**

## Authentication

Open the admin panel at http://localhost:8080 to configure.

### Method 1: Cookie Import

1. Visit https://aistudio.xiaomimimo.com and log in
2. Open **DevTools** → **Application** → **Storage → Cookies**
3. Find these three key cookies:
   - `serviceToken` — Service credential (most important)
   - `userId` — User ID (numeric)
   - `xiaomichatbot_ph` — Session identifier
4. Fill in the admin panel → Save

> **Tip:** serviceToken has a short validity (~24 hours) and must be re-imported after expiry.

### Method 2: cURL Import

1. Log in to aistudio.xiaomimimo.com
2. Open **DevTools** → **Network** panel
3. Send a message, find the `chat` request (SSE type)
4. Right-click → **Copy as cURL**
5. Paste into the admin panel → auto-parsed and saved

### Multi-Account Management

Support for adding **multiple accounts**, the proxy **auto-round-robins** between them:
- Each request pulls the next account from the pool → reduces per-account rate limit risk
- Support for connection testing, deletion, and replacing existing accounts
- Duplicate imports for the same userId auto-update (no duplicates)

## API Usage

### 1. List Models

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer sk-mimo"
```

Returns the model list showing all currently available MiMo official models.

### 2. Text Chat

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [
      {"role": "user", "content": "Hello, please reply in Chinese"}
    ]
  }'
```

### 3. Streaming Chat

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [
      {"role": "user", "content": "Tell me a story"}
    ],
    "stream": true
  }'
```

Returns standard SSE stream (`data: ...\n\n`), ending with `data: [DONE]\n\n`.

### 4. Multimodal (Vision)

Requires **omni/v2.5** models. Two image formats supported:

**URL method:**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }]
  }'
```

**Base64 method:**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
      ]
    }]
  }'
```

> **How it works:** The proxy auto-completes the 3-step upload flow: `genUploadInfo` to get signed URL → `PUT` to upload raw data → `resource/parse` to register parsing, then passes `multiMedias` into the chat API.

### 5. File Upload (Text Files)

Supports uploading text files (`.md`, `.txt`, etc.); MiMo reads the file content and responds based on it:

```bash
# Read file and encode as base64
BASE64=$(base64 -w0 yourfile.md)

curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"mimo-v2.5-pro\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [
        {\"type\": \"text\", \"text\": \"Summarize this file\"},
        {\"type\": \"file\", \"file\": {\"filename\": \"yourfile.md\", \"file_data\": \"$BASE64\"}}
      ]
    }]
  }"
```

> **Supported formats:** `.txt`, `.md`, `.py`, `.json`, `.yaml` and other plain text files. Files go through MiMo's native upload flow (`mediaType: "file"`); MiMo reads the available portion based on the token budget.

### 6. Function Calling

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [
      {"role": "user", "content": "What is the weather in Beijing today?"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Query weather for a specified city",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "City name"}
          },
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto"
  }'
```

On success, returns `finish_reason: "tool_calls"`, with `message.tool_calls` containing structured function calls:

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

### 7. Deep Thinking

Use the `reasoning_effort` parameter to enable deep thinking:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [
      {"role": "user", "content": "Prove that sqrt(2) is irrational"}
    ],
    "reasoning_effort": "high",
    "stream": true
  }'
```

Streaming responses include a `reasoning` field (corresponding to MiMo's `<think>` block), output separately from text content.

### 8. Model Discovery & Refresh

Model list is **auto-discovered at startup** from `https://aistudio.xiaomimimo.com/open-apis/bot/config`, no manual config needed.
> **⚠️ Ghost Model Note**: MiMo web client didn't clean up old model names. Dynamic discovery returns ~16 models (including v2-flash, v2-pro, v2-omni, v2.1 series, etc.), but **only the latest numbered series actually works** (currently `mimo-v2.5`, `mimo-v2.5-pro`, plus TTS/ASR). Old model names may fail or return empty responses — this is normal.

```bash
# Force refresh model list
curl -X POST http://localhost:8080/v1/models/refresh \
  -H "Authorization: Bearer sk-mimo"
```

### 9. Responses API

OpenAI's latest Responses API format, `/v1/responses` endpoint:

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [
      {"role": "user", "content": "Hello"}
    ]
  }'
```

Supports streaming (`"stream": true`), tool calling, deep thinking, system instructions, etc. See [Responses API](#responses-api) below for details.

## 9. Anthropic Messages API

MiMo2API v2.0.0 adds full Anthropic Messages API compatibility. Just swap the API endpoint and key:

```bash
# Non-streaming
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello"}
    ]
  }'

# Streaming
curl -N -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Tell me a story"}
    ]
  }'
```

### Supported Endpoints (9)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/messages` | POST | Send message (streaming/non-streaming, with thinking) |
| `/v1/messages/count_tokens` | POST | Count tokens (local estimation, requires tiktoken) |
| `/v1/messages/{message_id}` | GET | Retrieve stored message |
| `/v1/messages/batches` | POST | Create batch task |
| `/v1/messages/batches` | GET | List batches |
| `/v1/messages/batches/{batch_id}` | GET | Get batch details |
| `/v1/messages/batches/{batch_id}/cancel` | POST | Cancel batch |
| `/v1/messages/batches/{batch_id}/results` | GET | Download results JSONL |
| `/v1/messages/batches/{batch_id}` | DELETE | Delete batch |

### Anthropic Model Name Aliases

Tools like Claude Code CLI expect Anthropic-style model names and cannot directly use `mimo-*` native names. This proxy auto-maps them internally:

| Claude Model | → MiMo Internal |
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

> ⚠️ **Important:** It is recommended to use **2.5 series models**. MiMo web version only provides 2.5 series; using other legacy models may result in account suspension.

Also supports search/nothinking variants. MiMo native names (`mimo-*`) continue to work directly; `/v1/models` output is unchanged, not affecting other software.

### Authentication

Anthropic clients use the `x-api-key` header (RikkaHub auto-switches), also compatible with `Authorization: Bearer`:

```bash
# x-api-key (native Anthropic)
curl -H "x-api-key: sk-mimo" ...

# Authorization Bearer (backward compatible)
curl -H "Authorization: Bearer sk-mimo" ...
```

### Thinking Chain

MiMo's `<think>` tag content is auto-converted to Anthropic thinking blocks. Streaming responses output content blocks in **thinking → text → tool_use** order:

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

### Tool Calling

Supports Anthropic-format tool definitions (`input_schema` → OpenAI `parameters` auto-conversion):

```bash
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2.5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "What time is it?"}
    ],
    "tools": [{
      "name": "get_time",
      "description": "Get current time",
      "input_schema": {"type": "object", "properties": {}}
    }]
  }'
```

Returns Anthropic-format `tool_use` blocks:

```json
{
  "content": [
    {"type": "tool_use", "id": "tu_xxx", "name": "get_time", "input": {}}
  ],
  "stop_reason": "tool_use"
}
```

> **Note:** MiMo's tool calling is text-based TOOL_CALL format simulation, not native function calling.

## Tool Calling Details

MiMo API itself **does not** support OpenAI function calling format. This proxy implements it via **MiMoML prompt injection + 5-strategy extraction**:

### Prompt Injection

OpenAI tools definitions are converted to MiMoML (MiMo Markup Language) format and injected into the system message:

```xml
<|MiMoML|tool_calls>
  <|MiMoML|invoke name="get_weather">
    <|MiMoML|parameter name="city"><![CDATA[Beijing]]></|MiMoML|parameter>
  </|MiMoML|invoke>
</|MiMoML|tool_calls>
```

### 5 Extraction Strategies (by priority)

| Strategy | Format | Description |
|----------|--------|-------------|
| MiMoML | `<\|MiMoML\|tool_calls><\|MiMoML\|invoke name="X">...</\|MiMoML\|invoke></\|MiMoML\|tool_calls>` | Primary format, 7 noise variant tolerances |
| TOOL_CALL | `TOOL_CALL: name(key=value)` | Legacy format fallback |
| JSON | `{"name":"x","arguments":{...}}` | JSON block parsing |
| XML | `<tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>` | MiMo native XML |
| Mixed | `<function_call>{"name":"x","arguments":{...}}</function_call>` | XML-wrapped JSON |

### Fault Tolerance

- **Noise tolerance** — Supports missing pipes, duplicate `<`, fullwidth `｜`, hyphen `mimoml-`, and 7 other format variants
- **Fenced code blocks** — Automatically skips MiMoML examples inside markdown code blocks
- **JSON repair** — Auto-fixes unquoted keys, missing array brackets, illegal backslashes
- **Schema normalization** — Auto-converts non-string values to strings per tool schema
- **CDATA protection** — content/command/prompt text parameters retain original strings
- **Missing open tags** — Auto-restores wrapper when only closing tag is present

### Response Cleanup

After extraction, automatically cleans residual tool text from the response (MiMoML tags, XML tags, TOOL_CALL lines, JSON blocks, CDATA).

### Streaming Sieve

When tool calling is active and `stream: true`, the `tool_sieve` engine scans the MiMo response stream character by character, separating **content** from **tool call text** in real time:

- **Content** → immediately emitted as `delta.content` chunks, client displays progressively
- **Tool calls** → buffered until stream ends, then parsed and output as `tool_calls` in one shot

Non-sieve mode (no-tools streaming, non-streaming) is unaffected, maintaining the original logic. Detection supports three formats: `TOOL_CALL:`, `<tool_call>`, `<function=`, while whitelisting `<think>` deep thinking tags.


## TTS Speech Synthesis

Endpoint: `POST /v1/audio/speech` (OpenAI Compatible)

Text-to-speech converts input text into natural, fluent speech output. Supports preset voices, voice design, voice cloning, and more.

> 📖 [Official Documentation](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)

### Supported Models

| Model ID | Function | Voice Source | Notes |
|----------|----------|--------------|-------|
| `mimo-v2.5-tts` | Use preset premium voices | Preset voice list | Supports singing mode, no voice design/clone |
| `mimo-v2.5-tts-voicedesign` | Customize voice via text description | Auto-generated from text | No singing, preset voices, or voice clone |
| `mimo-v2.5-tts-voiceclone` | Clone any voice from audio sample | Audio sample cloning | No singing, preset voices, or voice design |

### Basic Usage

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts",
    "input": "Hello, world!",
    "voice": "alloy"
  }' \
  --output speech.wav
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| model | string | mimo-v2.5-tts | Model name |
| input | string | (required) | Text to synthesize |
| voice | string | alloy | Voice name (preset or base64 audio) |
| speed | float | 1.0 | Speech rate (0.5-2.0) |
| response_format | string | wav | Output format |
| style | string | (empty) | Voice description for voicedesign model |

### Preset Voices

Only `mimo-v2.5-tts` model supports preset voices.

| OpenAI Voice | MiMo Voice ID | Language | Gender |
|--------------|---------------|----------|--------|
| alloy | BingTang | Chinese | Female |
| echo | MoLi | Chinese | Female |
| fable | BaiHua | Chinese | Male |
| onyx | SuDa | Chinese | Male |
| nova | Mia | English | Female |
| shimmer | Chloe | English | Female |
| - | Milo | English | Male |
| - | Dean | English | Male |

> 💡 You can also use MiMo native voice IDs (e.g., `mimo_default`) as the `voice` parameter.

### Voice Design (voicedesign)

Generate custom voice from text description, no reference audio needed:

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voicedesign",
    "input": "Hello, world!",
    "voice": "alloy",
    "style": "A gentle, sweet female voice, moderate speed"
  }' \
  --output speech.wav
```

### Voice Clone (voiceclone)

Clone any voice from audio sample, `voice` parameter takes reference audio in base64:

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voiceclone",
    "input": "This is speech generated with cloned voice.",
    "voice": "data:audio/wav;base64,UklGRi..."
  }' \
  --output speech.wav
```

### voicedesign vs voiceclone

| Feature | voicedesign | voiceclone |
|---------|-------------|------------|
| Voice source | Text description | Audio sample |
| Reference audio needed | No | Yes |
| Control method | `style` parameter | `voice` parameter |
| Use case | Quick style generation | Precise voice replication |

---

## ASR Speech Recognition

Endpoint: `POST /v1/audio/transcriptions` (OpenAI Whisper Compatible)

Speech recognition converts input audio into text output. Suitable for meeting transcription, lyrics recognition, dialect transcription, noisy recordings, and more.

> 📖 [Official Documentation](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)

### Supported Model

Currently only supports `mimo-v2.5-asr` model.

### Core Capabilities

- **Multi-language** — Chinese/English bilingual recognition with automatic language detection, native support for Cantonese, Wu, Minnan, Sichuan dialect
- **High robustness** — Stable recognition in noisy, far-field, overlapping speech conditions; supports lyrics with accompaniment
- **Precise recognition** — Accurately recognizes poetry, technical terms, names/places; auto-generates punctuation

### Basic Usage

```bash
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@audio.mp3" \
  -F "language=auto"
```

Returns:

```json
{"text": "Recognized text content"}
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| file | file | (required) | Audio file (wav/mp3) |
| model | string | mimo-v2.5-asr | Model name (optional) |
| language | string | auto | Language code (auto/zh/en etc.) |
| response_format | string | json | Response format: json or text |

### Supported Audio Formats

Supports `wav` and `mp3` format audio files. Base64 encoded string size limit is **10MB**.

| Format | MIME Type |
|--------|-----------|
| wav | audio/wav |
| mp3 | audio/mpeg |

### Examples

```bash
# Recognize Chinese audio
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@meeting.wav" \
  -F "language=zh"

# Recognize English audio
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@speech.mp3" \
  -F "language=en"

# Auto-detect language
curl -X POST http://localhost:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer ***" \
  -F "file=@audio.wav" \
  -F "language=auto"
```

---
## Responses API

Endpoint: `POST /v1/responses`

MiMo2API fully implements the OpenAI Responses API format, supporting the same underlying capabilities as Chat Completions.

### Differences from Chat Completions

| | Chat Completions | Responses API |
|---|---|---|
| Endpoint | `/v1/chat/completions` | `/v1/responses` |
| Message field | `messages` | `input` |
| System instructions | `messages[role=system]` | `instructions` |
| Tool format | `tool.function.name` | `tool.name` |
| Response format | `choices[0].message` | `output[]` array |
| Thinking content | `reasoning_content` | `output[type=reasoning]` |
| Tool call | `message.tool_calls` | `output[type=function_call]` |

### Basic Usage

```bash
# Non-streaming
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "Hello"}]
  }'

# Streaming (SSE)
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "Tell me a story"}],
    "stream": true
  }'
```

### Tool Calling

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "input": [{"role": "user", "content": "What time is it?"}],
    "tools": [{
      "type": "function",
      "name": "get_time",
      "description": "Get current time",
      "parameters": {
        "type": "object",
        "properties": {
          "timezone": {"type": "string"}
        }
      }
    }]
  }'
```

> **Tool format note:** Responses API `tools` has no `function` nesting layer; `name` is at the top level (unlike Chat Completions' `tool.function.name`). MiMo2API supports both formats.

### Response Format

```json
{
  "output": [
    {
      "type": "reasoning",
      "summary": [{"type": "summary_text", "text": "Model thinking..."}]
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
      "content": [{"type": "output_text", "text": "The current time is..."}]
    }
  ]
}
```

`output` is ordered: reasoning (if present) → function_call (if present) → message.

## Management Commands

```bash
# Foreground (Ctrl+C to stop)
./venv/bin/python main.py

# Background
nohup ./venv/bin/python main.py > mimo.log 2>&1 &
echo $! > mimo.pid

# Stop from PID file
kill $(cat mimo.pid)

# Stop by process name
pkill -f "python main.py"

# Watch real-time logs
tail -f mimo.log

# Check process status
ps aux | grep "python main.py"

# Check port usage
lsof -i :8080
```

**After Startup:**

| Address | Description |
|---------|-------------|
| `http://localhost:8080` | Web admin panel (configure accounts) |
| `http://localhost:8080/v1` | OpenAI + Anthropic compatible API root |
| `http://localhost:8080/docs` | Swagger API documentation |
| `http://localhost:8080/v1/messages` | Anthropic Messages API |
| `http://localhost:8080/v1/responses` | OpenAI Responses API |

## Project Structure

```
MiMo2API/
├── main.py                  # Entry point, FastAPI app creation + uvicorn startup
├── deploy.sh                # One-click deploy script (install deps, init config)
├── requirements.txt         # Python dependencies
├── config.example.json      # Config file template
├── config.json              # Actual config (.gitignore, contains credentials)
├── app/
    ├── __init__.py
    ├── routes.py            # API routes (chat/models/admin panel/account CRUD)
    ├── anthropic_routes.py  # Anthropic Messages API routes (9 endpoints)
    ├── anthropic.py         # Anthropic ↔ OpenAI format conversion core
    ├── batch.py             # Anthropic batch tasks + count_tokens
    ├── models.py            # OpenAI compatible data models (Pydantic)
    ├── mimo_client.py       # MiMo API client (HTTP SSE stream handling)
    ├── config.py            # Config management (multi-account, thread-safe, round-robin)
    ├── utils.py             # Utility functions (cURL parsing, image upload, message building)
    ├── tool_sieve.py        # Streaming sieve engine (real-time tool call / content separation)
    ├── tool_call.py         # Tool calling (prompt injection + 5-strategy extraction + cleanup)
    ├── usage_store.py       # Usage data persistence
    ├── session_store.py     # Session management (fingerprint-based conversationId continuation)
    ├── response_store.py    # Responses API record persistence
    └── web/
        └── index.html       # Web admin panel
```

## Configuration Reference

Full `config.json` configuration:

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

| Config Item | Description | Default |
|-------------|-------------|---------|
| `api_keys` | Comma-separated API key list | `sk-mimo` |
| `mimo_accounts` | MiMo account list (multiple allowed) | `[]` |
| `models` | Custom model list (empty = auto-discovery) | `[]` |

**Environment variable:** `PORT` — listening port (default `8080`)

## Dependencies

- **Python 3.10+**
- FastAPI 0.115
- uvicorn 0.32
- httpx 0.27
- Pydantic v1

```bash
pip install -r requirements.txt
```

## Limitations & Known Issues

| Limitation | Description |
|------------|-------------|
| Token validity & silent downgrade | serviceToken expires in ~24h. After expiry, basic chat (2.5 series) may still work, but **mimo-v2.5 multimodal vision** silently fails. Admin panel "Test Connection" only checks the normal chat endpoint and cannot detect this issue. Fix requires logging out and re-logging in via the web UI; see FAQ below |
| Multimodal models | `mimo-v2.5` support vision; all models support file upload and image OCR text extraction |
| Concurrency limit | Depends on MiMo server-side limits (typically 1-2 concurrent/account); multiple accounts help mitigate |
| No Embeddings | Only Chat Completions and Responses endpoints implemented |
| Non-streaming uses SSE internally | MiMo API only provides SSE streams; non-streaming requests buffer all SSE events then merge |

## FAQ

**Q: Why do I get 401 "invalid api key"?**
A: Check that the `Authorization` header carries the correct API Key. Default is `sk-mimo`, configurable in `config.json`.

**Q: Why 503 "no mimo account"?**
A: No accounts configured in the admin panel, or all accounts have expired. Log in at http://localhost:8080 and add valid accounts.

**Q: Image upload fails? Model says "no image seen"?**
A: Usually caused by abnormal server-side session state; simply re-obtaining cookies is ineffective. Correct steps:
1. Open https://aistudio.xiaomimimo.com in browser
2. **Log out** (must log out, not just refresh)
3. Log back in
4. Re-import cookies in the admin panel
If the account is restricted, switch to another account.

**Q: mimo-v2.5 multimodal vision suddenly fails, but test connection shows OK?**
A: This is the **silent downgrade** phenomenon after serviceToken expiry. MiMo API enforces stricter credential validation for multimodal vision than for regular chat. After token expiry:
- Basic chat (flash/pro) may still work normally
- Admin panel "Test Connection" also shows OK (it only checks the normal chat endpoint)
- But multimodal vision returns nonsense results or errors

**Symptom check:** If normal chat works but multimodal vision suddenly fails, it's likely credential expiry.
**Fix:** Same as above — log out from the web UI, log back in, re-import new cookies. If new cookies still don't work, try another account.

**Q: tool_call extraction not working?**
A: Check logs to confirm response content. If MiMo doesn't output tool calling format as expected, the prompt may not be clear enough, or the model may have limited comprehension. Recommend using `mimo-v2.5-pro` for tool calling.

**Q: Can this be deployed on a public server?**
A: Yes, but change the default API Key (`sk-mimo` is too simple). Recommend using Nginx reverse proxy + HTTPS.

## License

MIT License

---

**Credits:**

- Xiaomi MiMo AI Studio for providing the base API service.
- [GoblinHonest/mimo2api_mimoapi](https://github.com/GoblinHonest/mimo2api_mimoapi) — Session management (message fingerprint-based MiMo conversationId continuation) design reference.
- [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) — DSML tool calling format and streaming sieve engine design reference.
