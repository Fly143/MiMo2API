# MiMo2API Android 版

将小米 MiMo AI Studio 网页版代理服务打包为独立 Android APK（Chaquopy + WebView）。

## 特性

- **点开即用** — 打开 App 直接进入管理后台（右下角「登录小米账号」按钮进入内置浏览器登录）
- **内置浏览器登录** — 登录小米 aistudio 后自动抓取 cookie（`xiaomichatbot_serviceToken` / `userId` / `xiaomichatbot_ph`）并导入
- **WebView 管理后台** — 账号管理、API Key 配置、用量统计
- **pydantic v1** — 降级以适配 Chaquopy（无 pydantic-core 预编译）

## 环境要求

| 依赖 | 版本 |
|---|---|
| Android SDK | compileSdk 35 (Android 15) |
| JDK | 17 |
| Gradle | 8.9（wrapper 自动下载，无需预装） |
| Python 依赖 | 纯 Python 包（pydantic<2），构建时由 Chaquopy 自动安装 |

## 构建

### 环境要求（必读）

| 工具 | 版本 / 说明 | 原因 |
|---|---|---|
| **JDK 17+** | 不能是 JRE。AGP 8.9 强制 JDK 17，`jlink` 只在 JDK 里 | 用 JRE 会在 `JdkImageTransform` 报 `jlink.exe does not exist` |
| **Android SDK** | compileSdk 35（Android 15），含 `build-tools` 与 `platforms/android-35` | 构建必需 |
| **Gradle** | 8.9（wrapper 自动下载，无需预装） | — |
| **Python 依赖** | 纯 Python 包（pydantic<2），构建时由 Chaquopy 自动安装 | 无需手动装 |

> 验证 JDK：`java -version` 应显示 `17.x` 且路径指向 **JDK**（含 `bin/jlink`）。
> 设置 `JAVA_HOME` 指向该 JDK，否则 Gradle 默认可能找到 JRE 而失败。

### 步骤

```bash
# 1. 克隆（fork 后把地址换成你自己的仓库）
git clone -b android https://github.com/Fly143/MiMo2API.git
cd MiMo2API

# 2. 设置 Android SDK 位置
#    方式 A（推荐，环境变量，无需写文件）：
#      Windows:  set ANDROID_HOME=C:\path\to\android\sdk
#      macOS/Linux:  export ANDROID_HOME=/path/to/android/sdk
#    方式 B（写 local.properties，仓库已 gitignore，不会泄露）：
#      Windows (cmd):  echo sdk.dir=C:\path\to\android\sdk > local.properties
#      macOS/Linux:    echo sdk.dir=/path/to/android/sdk > local.properties
#    注意：Windows 上路径用单个反斜杠（C:\Android\sdk），不要转义冒号。

# 3. 构建 release APK（无 keystore 时自动使用 debug 签名）
./gradlew assembleRelease
# 或 debug 版
./gradlew assembleDebug

# 产物
app/build/outputs/apk/release/app-release.apk
```

> **Windows 用 cmd.exe 跑 gradlew.bat**（不要用 MSYS/Git-Bash 跑 `./gradlew`）：
> 后者会把项目路径转成 `/c/...` 风格传给 Java，易触发 `IOException: 文件名、目录名或卷标语法不正确`。
> 在原生 `cmd` 里 `cd /d C:\path\to\MiMo2API` 后执行 `gradlew.bat assembleRelease` 最稳。

> **lint 不影响产物**：`app/build.gradle` 已设 `lintOptions { checkReleaseBuilds false; abortOnError false }`，
> release 构建不会被 `lintVital` 阻断。


## 架构

```
┌─ Android App ────────────────────────────┐
│  MainActivity (WebView 管理后台)          │
│    └─ http://127.0.0.1:8000/admin        │
│  ServerService (前台服务)                 │
│    └─ Chaquopy Python 后端                │
│       └─ uvicorn @ 127.0.0.1:8000        │
│          └─ app/main.py (MiMo 代理)      │
└──────────────────────────────────────────┘
```

- 端口：**8000**（DeepSeek 版用 8001，可同时运行）
- 管理后台认证：`admin` / `admin`（可在 config.json 修改）

## 与主分支的关系

本分支（`android`）为 Android 打包版。主分支（`main`）是桌面版 Python 服务。
同步流程：主分支更新后，将 `main.py`、`web/` 等 Python 代码同步到
`app/src/main/python/` 并重新构建 APK。

## 主要文件

| 文件 | 说明 |
|---|---|
| `app/src/main/python/main.py` | MiMo 代理主逻辑 |
| `app/src/main/python/android_boot.py` | Android 启动器（路径重定向、uvicorn） |
| `app/src/main/java/.../ServerService.java` | 前台服务，启动 Python 后端 |
| `app/src/main/java/.../MainActivity.java` | WebView 管理后台 |
| `app/src/main/java/.../LoginActivity.java` | 内置浏览器登录页（cookie 自动导入） |
| `app/src/main/java/.../AdminCreds.java` | 管理后台认证凭据 |
