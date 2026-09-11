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
from app.anthropic_routes import router as anthropic_router
from app.batch import init_batch_storage as init_anthropic_batches

app = FastAPI(
    title="Mimo2API",
    description="将小米 Mimo AI 转换为 OpenAI + Anthropic 兼容 API（Chat / Responses / Anthropic Messages）",
    version="2.6.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_discover_models():
    import os as _anthropic_os
    from app.batch import init_batch_storage as _mimo_init_batch_storage
    _mimo_init_batch_storage(_anthropic_os.path.join(_anthropic_os.path.dirname(_anthropic_os.path.abspath(__file__)), ".anthropic_batches"))
    try:
        await _do_discover()
        print("模型预探测完成")
    except Exception as e:
        print(f"模型预探测失败（不影响服务）: {e}")

    print("[启动] 后台清理过期会话...")
    import threading
    threading.Thread(target=_cleanup_old_sessions, daemon=True).start()


def _cleanup_old_sessions():
    """后台清理过期会话，每个删除间隔 10 秒。"""
    import time, asyncio
    async def _run():
        try:
            from app.session_store import get_expired_sessions, remove_session
            from app.mimo_client import MimoClient
            from app.config import config_manager
            expired = get_expired_sessions()
            if not expired:
                return
            print(f"[Cleanup] Found {len(expired)} expired sessions, deleting with 10s delay...")
            by_account = {}
            for account_label, conv_id, model, days_ago in expired:
                by_account.setdefault(account_label, []).append((conv_id, days_ago))
            deleted = 0
            for account_label, conv_items in by_account.items():
                acc = None
                for a in config_manager.config.mimo_accounts:
                    if a.user_id == account_label:
                        acc = a
                        break
                if not acc:
                    continue
                client = MimoClient(acc)
                for conv_id, days_ago in conv_items:
                    try:
                        if await client.delete_conversations([conv_id]):
                            remove_session(account_label, conv_id)
                            deleted += 1
                            print(f"[Cleanup] Deleted: {conv_id[:12]}... ({days_ago}d old)")
                    except Exception:
                        pass
                    time.sleep(10)
            print(f"[Cleanup] Done: {deleted}/{len(expired)}")
        except Exception as e:
            print(f"[Cleanup] Failed: {e}")
    asyncio.run(_run())


app.include_router(router)
app.include_router(anthropic_router)

import os
_anthropic_batch_dir = os.path.join(os.path.dirname(__file__), ".anthropic_batches")
init_anthropic_batches(_anthropic_batch_dir)

web_dir = Path(__file__).parent / "web"


def main():
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"""
Mimo2API Android/Python
  地址: http://{host}:{port}
  管理: http://{host}:{port}
  API:  http://{host}:{port}/v1/chat/completions
""")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
