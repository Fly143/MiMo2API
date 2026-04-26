"""Mimo2API Python版本 - 主程序入口"""

import os
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from app.routes import router, _do_discover
from app.config import config_manager

# 创建FastAPI应用
app = FastAPI(
    title="Mimo2API",
    description="将小米 Mimo AI 转换为 OpenAI 兼容 API",
    version="1.0.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_discover_models():
    """服务启动时预探测模型，避免首次请求返回3个硬编码模型"""
    try:
        await _do_discover()
        print("✅ 模型预探测完成")
    except Exception as e:
        print(f"⚠️ 模型预探测失败（不影响服务）: {e}")


# 注册路由
app.include_router(router)

# 静态文件目录
web_dir = Path(__file__).parent / "web"

# 管理页面由 routes.py 中的 router 处理（/ 和 /admin）


def main():
    """主函数"""
    # 获取端口配置
    port = int(os.getenv("PORT", "8080"))

    print(f"""
╔══════════════════════════════════════════════════════════╗
║                    Mimo2API Python                       ║
║          将小米 Mimo AI 转换为 OpenAI 兼容 API           ║
╚══════════════════════════════════════════════════════════╝

🚀 服务器启动中...
📍 地址: http://localhost:{port}
📊 管理界面: http://localhost:{port}
📡 API端点: http://localhost:{port}/v1/chat/completions
📖 API文档: http://localhost:{port}/docs

配置信息:
  - API Keys: {len(config_manager.config.api_keys.split(','))} 个
  - Mimo账号: {len(config_manager.config.mimo_accounts)} 个

按 Ctrl+C 停止服务器
""")

    # 启动服务器
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
