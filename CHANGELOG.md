# 更新日志（Changelog）

本文件记录 MiMo2API 的所有重要变更。

## [v2.6.6] — 2026-09-12

### 变更
- **去掉硬编码 `temperature`/`topP`** — 仅显式传入时写入请求体，其余交上游
- **HTTP 超时默认 600s** — 可用 `MIMO_CLIENT_TIMEOUT` 覆盖

## [v2.6.5] — 2026-09-11

### 修复
- **CORS** — `allow_credentials` 改为 `false`（与 `allow_origins=["*"]` 组合在浏览器规范上不安全）

## [v2.6.4] — 2026-09-11

### 文档
- **`build_tool_prompt` 注释与代码一致性修正** — 之前注释说
  "passthrough=True 时跳过格式说明书，直接嵌入原始工具定义 JSON"，但代码同时在 prompt 里塞了
  一段英文指令引导模型用原生格式（实际 MiMo 网页端上游根本不支持原生 tool_calls，prompt 必须
  引导模型输出文本协议标记）。
  重写注释：明确 passthrough=True 仍走文本协议（只是简化格式说明书 + 嵌入工具 JSON），
  passthrough=False 走完整 MiMoML 说明书。
  与 workbuddy-desktop-api / xiaomi-mimo-desktop-api 的 passthrough=True 语义不同：
  后两者上游原生支持 OpenAI tool_calls，passthrough=True 直接 return ""。

## [v2.6.3] — 2026-09-11

### 修复
- **清理 Chaquopy 打包产物副本 `app/src/`** — 该目录是 `MiMo2API-android/` 项目
  的 Chaquopy 源码布局（`src/main/<sourceSet>/`）被错误复制到本仓：被跟踪的 8 个文件
  都是旧版本副本或空文件（`mimo_client.py`/`routes.py` 各 0 行），与根 `app/*.py` 重复且滞后，
  仓内代码/文档零引用。
  处理：`git rm -r app/src`（磁盘同步清理）+ `.gitignore` 加入 `app/src/`。
  体积减少 701K；仓克隆与索引均不再被污染。

## [v1.0.3] — 2026-09-11

### 修复
- **原生 tool_calls 恒为空（严重）** — `clean_tool_text` 在 `extract_tool_call` 之前执行，
  抹掉 `TOOL_CALL:` / `<tool_call>` / `<|MiMoML|...>` 等标记，导致 extract 永远匹配不到。
  实测三种格式修复前提取结果全为 None，修复后全部正确。
  影响 chat completions 非流式与 responses 非流式两条路径
- **流式响应同一条消息同时含 content 与 tool_calls** — 带 tools 时正文边收边发，
  工具调用要到流结束才确定，与非流式（`content=None`）行为不一致。
  改为带 tools 时正文先缓冲：命中工具调用则丢弃缓冲正文只发 tool_calls，否则收尾补发
- **会话上下文被过早重置** — `update_tokens` 对 `prompt_tokens` 累加，但上游返回的是
  本次请求的完整上下文长度（已含历史）而非增量，累加会线性放大并提前触碰
  `TOKEN_THRESHOLD`，触发清屏重建、丢失多轮上下文。改为记录峰值
- **Anthropic 流式异常收尾丢失 tool_use** — 上游流未带 `finish_reason` 就结束时，
  兜底分支只发 `end_turn`，已攒好的 `tool_call_slots` 被静默丢弃

### 附带修复
- `StreamSieve._split_safe` 工具标记前缀识别大小写敏感，小写标记跨 chunk 切断时会把
  残片当正文吐出，改为大小写不敏感比对

## [v1.0.4] — 2026-09-11

### 变更
- **移除代理层自作主张的 `max_tokens` 默认值** — preview 模型不再硬填 4096，
  Anthropic 转换层不再兜底 4096；未显式指定时透传，由上游/模型自行决定输出长度

### 实测说明（重要）
- **上游 `/api/route` 并不遵守 `max_tokens`**：
  `max_tokens=1` 仍返回 601 字符正文，`20` / `50` / `4096` 输出长度无差异
  （均 ~500-950 字符，`finish_reason` 全部为 `stop`）
- 同一「写 8000 字长文」请求重复执行，传与不传 4096 均出现
  **5798 / 20417 / 20454** 的波动，属模型自身随机性，与 `max_tokens` 无关
- 因此本次改动**不改变实际行为**；目的是让代理层不再猜测上游语义 ——
  同为代理层的 WorkBuddy 上游语义相反（`max_tokens` 是 reasoning + 正文的合计预算，
  硬填 4096 会导致思考链吃光预算、正文 0 字符），硬编码默认值在不同上游间不可移植

## [v2.6.0] — 2026-09-11

### 安全
- **凭证加密落盘** — `serviceToken` / `userId` / `xiaomichatbot_ph` / `admin_password` 使用 Fernet 加密（`enc:v1:`），密钥在 `.secret_key`；旧明文配置自动迁移
- **导入探活无残留** — Cookie/cURL 导入与测试连接使用临时 conversation，验证后删除，不在账号里留下 `hi` 会话
- **`/api/config` 局部更新** — 只传要改的字段，不会清空已导入账号

### 修复
- **Anthropic 模型映射** — 补全 Claude 4.7 / 4.5 别名；支持 `-YYYYMMDD` / `-latest` 后缀；未知 `claude-*` 启发式落到 2.5 系列（仅映射 `mimo-v2.5` / `mimo-v2.5-pro`）

### 文档 / 仓库
- README 补充管理后台默认账号密码、改密方式、`.secret_key` 备份说明
- 停止跟踪本地 `usage.json`

## [v2.2.5] — 2026-05-12

### 新增
- **多语言支持** — 管理面板支持中英双语切换（🌐 EN/中 按钮），涵盖所有 UI 文本
- **英文 README** — 新增 `README_EN.md` 英文版文档，中英互链

## [v2.2.4] — 2026-05-11

### Fixed
- **换行符保留** — `clean_tool_text` 不再 strip 末尾空白

## [v2.2.3] — 2026-05-11

### Fixed
- **工具标签泄漏补全** — `clean_tool_text` 覆盖所有文本输出路径（流式/非流式、OpenAI/Anthropic），新增 `_clean_response_text()` 合并清洗函数。MiMo 原生 `<tool_call>` / `<function=` / `<parameter=` / `<invoke>` 格式全部兜底清理

## [v2.2.2] — 2026-05-11

### Fixed
- **skill 工具参数兼容** — MiMo 模型偶尔把 `skill_view` 的参数名写成 `skill_name`，现自动重映射为 `name`，兼容两种写法。同时覆盖 `skill_manage`、`use_skill`
- **MiMoML 格式泄漏** — 模型输出 MiMoML 格式模板示例（占位符工具名）时，标签不再泄漏到响应正文。`extract_tool_call` 无匹配回退时也清理 MiMoML 残留
- **StreamSieve 清理** — Sieve 捕获 MiMoML 但未解析到工具调用时，使用 `extract_tool_call` 清理后的文本而非原始捕获缓冲

## [v2.3.2] — 2026-05-08

### Added
- **JSON 修复** — `_repair_loose_json()`：未加引号 key、缺失数组括号、非法反斜杠自动修复
- **Schema 归一化** — `_coerce_string_params()`：根据 tool schema 将非字符串值自动转为字符串
- **空参数过滤** — `_has_meaningful_value()`：跳过无实际内容的工具调用参数
- **CDATA 参数保护** — content/command/prompt 等文本参数保留原始字符串
- **CDATA 内嵌围栏块** — `_extract_cdata_safe()`：围栏代码块内的 ]]> 不误判
- **`<br>` 归一化** — `_normalize_br()`：CDATA 中的 `<br>` 标签自动转为换行符

## [v2.3.1] — 2026-05-08

### Added
- **MiMoML 噪声容错** — `strip_mimoml()` 支持 7 种格式变体（缺管道、重复 <、全宽、连字符等）
- **围栏代码块保护** — 自动跳过 markdown 代码块内的 MiMoML 示例
- **结构化参数恢复** — `<item>` 子节点转为数组，嵌套 XML 还原对象
- **缺失开标签修复** — 有关闭标签无开头时自动补回
- **HTML 实体解码** — `&lt;` `&gt;` `&amp;` 等自动还原

### Changed
- 策略精简 7→5：删除中文格式和自由文本策略

## [v2.3.0] — 2026-05-08

### Added
- **MiMoML 工具调用格式** — 新增 MiMoML（MiMo Markup Language）格式作为主要工具调用协议
  - `<|MiMoML|tool_calls><|MiMoML|invoke name="X"><|MiMoML|parameter name="K"><![CDATA[V]]></|MiMoML|parameter></|MiMoML|invoke></|MiMoML|tool_calls>`
  - CDATA 包裹解决转义问题，多工具调用天然支持
  - 提升 Roo Code 等 DeepSeek 生态客户端的兼容性
- **策略0：MiMoML 提取** — `strip_mimoml()` 将 MiMoML 转为标准 XML 后解析
- **致谢 ds2api** — [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) DSML 格式设计参考

### Changed
- **工具提示词** — `build_tool_prompt()` 从 `TOOL_CALL:` 格式切换到 MiMoML 格式
- **`clean_tool_text()`** — 新增 MiMoML/CDATA 标签清理正则

## [v2.1.0] — 2026-05-07

### Added
- **Anthropic 模型名映射** — Claude Code CLI 等工具可使用 Anthropic 风格模型名（如 `claude-sonnet-4-6`），内部自动映射为对应 MiMo 模型
  - `claude-opus-4-6` → `mimo-v2-pro`
  - `claude-sonnet-4-6` → `mimo-v2-flash`
  - `claude-haiku-4-5` → `mimo-v2-flash`
  - 支持 search/nothinking 变体及 Claude 3.x/4.x 历史名
- MiMo 原生名（`mimo-*`）继续直接可用，`/v1/models` 返回不变

## [v2.0.0] — 2026-05-06

### Added
- **Anthropic Messages API 全兼容** — 新增 9 个 Anthropic 端点：`/v1/messages`（流式/非流式）、count_tokens、message CRUD、batch 全流程
- **多账号管理** — Web 面板增删账号、轮询负载均衡
- **TTS 语音合成**（no-tools）— 声线克隆、音色设计、导演模式

### Changed
- 路由拆分为 `app/anthropic_routes.py`（APIRouter 模式）
- `app/anthropic.py` + `app/batch.py` 模块化

## [Unreleased]

### Changed
- CHANGELOG.md 初始化
- README 补充静默降级 FAQ

---

## [v1.0.0] — 2026-05-04

### Added
- **工具调用** — 6 种提取策略覆盖 TOOL_CALL、JSON、MiMo 原生 XML、`<function_call>`、自由文本匹配、中文 `[调用工具:]` 格式
- **流式筛分（tool_sieve）** — 实时分离流式响应中的正文与工具调用，无需全量缓冲再输出
- **会话管理** — SHA256 消息指纹续接 MiMo conversationId，跨请求保持上下文
- **按模型上下文窗口** — 根据官方 Pricing 页设置精确的 `context_length`/`max_output_tokens`（v2.5-pro/v2-pro/v2.5 为 1M，v2-flash/v2-omni 为 256K）
- **文本文件上传** — 原生 MiMo resource 上传流程（genUploadInfo → PUT OSS → resource/parse），支持 .md/.txt/.py/.json 等
- **用量统计** — 按模型分组的 Token 追踪，Web 面板可视化，支持今日/本周/全部筛选，清空按钮
- **Web 管理面板** — 多 Tab 布局（cURL 导入、Cookie 导入、账号列表、用量统计、API Key 管理）

### Changed
- **双分支架构** — `main`（工具调用）和 `no-tools`（纯对话 + TTS）独立维护
- **工具提示词精简** — 从 30+ 行降到 ~10 行，移到 query 末尾，每次最多注入 6 个工具
- **三轮注入策略** — 首轮完整提示词，后续轮只列工具名（不加行为指令），防止死循环
- **查询格式重排** — 用户消息在前，工具信息在后，跳过 system 消息（MiMo 不支持角色分离）
- **模型列表** — 从 MiMo API 动态发现，未知模型过滤

### Fixed
- **Pydantic v1 兼容** — `model_dump()` 改为 `dict()`（项目依赖 pydantic<2）
- **TOOL_CALL 文本泄露** — 流式筛分实时截获并过滤工具调用文本
- **camelCase 工具名不匹配** — `_resolve_tool_name()` 四级匹配（直接/忽略大小写/驼峰转蛇形/模糊）
- **工具结果标签泄露** — `_strip_tool_result_blocks()` 覆盖 3 种格式：`[TOOL_RESULT]`、`[tool_result id=xxx]`、`<tool_result>`
- **工具调用死循环** — 三轮注入策略防止重复调用同一工具
- **工具提示词被截断丢弃** — 截断后重新插入工具信息
- **空参数工具调用失败** — 正则 `(.+?)` → `(.*?)` 允许 `getTimeInfo()`
- **流式沉默间隙** — `_safe_flush()` 只保留 `<think>`/`</think>` 部分后缀，不吞内容
- **图片模型劫持** — 移除强制切到 omni 的逻辑，用户选择什么模型就走什么模型
- **cURL 添加账号失败** — `update_config()` 增加字段过滤，拒绝 `token_masked`
- **RikkaHub 流式延迟** — reasoning 实时流式，有工具时仅正文缓冲
- **Cookie 字符串解析** — 支持粘贴整段 `key=value; key=value` Cookie header
- **保存按钮无反馈** — 所有保存按钮增加 disabled + loading 文本

### 已知问题
- serviceToken 约 24 小时过期，需网页端退出重新登录（仅刷新 Cookie 无效）
- **静默降级：** Token 过期后，基础聊天（flash/pro）和"测试连接"仍显示正常，但 `mimo-v2.5` / `mimo-v2-omni` 多模态识图会静默失效。如果只聊天空正常但识图不工作，优先怀疑凭证过期
- MiMo 服务端并发限制：约 1-2 请求/账号
- 不支持 Embeddings 端点
- 非原生 function calling（通过文本提示模拟）

---

## [0.x] — 初期开发阶段

从 [Water008/MiMo2API](https://github.com/Water008/MiMo2API) fork 后的早期改版（网页直接上传文件，无 git 历史记录），包含以下功能沉淀：

- OpenAI 兼容 `/v1/chat/completions`、`/v1/models` 端点
- 多账号轮询负载均衡
- Cookie / cURL 凭证导入 + Web 管理面板
- 图片上传（genUploadInfo → PUT → resource/parse → multiMedias）
- Think 块分离（`<think>`/`</think>`）
- Termux/Android 部署脚本
- 功能文档 README

---

## 分支说明

| 分支 | 功能 |
|------|------|
| `main` | 工具调用（6 策略）、流式筛分、会话管理、文件上传 |
| `no-tools` | 纯对话代理 + TTS（语音合成、音色设计、语音克隆、导演模式） |

日常使用推荐 no-tools 分支（上下文更干净，输出质量更高）。如需 TTS 功能直接使用 no-tools。
