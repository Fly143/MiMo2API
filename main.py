"""Mimo2API Python鐗堟湰 - 涓荤▼搴忓叆鍙?""

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

# 鍒涘缓FastAPI搴旂敤
app = FastAPI(
    title="Mimo2API",
    description="灏嗗皬绫?Mimo AI 杞崲涓?OpenAI + Anthropic 鍏煎 API锛圕hat / Responses / Anthropic Messages锛?,
    version="2.6.6"
)

# 娣诲姞CORS涓棿浠?app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_discover_models():
    import os as _anthropic_os
    from app.batch import init_batch_storage as _mimo_init_batch_storage
    _mimo_init_batch_storage(_anthropic_os.path.join(_anthropic_os.path.dirname(_anthropic_os.path.abspath(__file__)), ".anthropic_batches"))
    """鏈嶅姟鍚姩鏃堕鎺㈡祴妯″瀷锛岄伩鍏嶉娆¤姹傝繑鍥?涓‖缂栫爜妯″瀷"""
    try:
        await _do_discover()
        print("鉁?妯″瀷棰勬帰娴嬪畬鎴?)
    except Exception as e:
        print(f"鈿狅笍 妯″瀷棰勬帰娴嬪け璐ワ紙涓嶅奖鍝嶆湇鍔★級: {e}")

    # 鍚庡彴娓呯悊杩囨湡浼氳瘽锛堥伩鍏嶉鎺э級
    print("[鍚姩] 鍚庡彴娓呯悊杩囨湡浼氳瘽...")
    import threading
    threading.Thread(target=_cleanup_old_sessions, daemon=True).start()


def _cleanup_old_sessions():
    """鍚庡彴娓呯悊杩囨湡浼氳瘽锛屾瘡涓垹闄ら棿闅?10 绉掋€?""
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


# 娉ㄥ唽璺敱
app.include_router(router)
app.include_router(anthropic_router)

# 鍒濆鍖?Anthropic batch 瀛樺偍
import os
_anthropic_batch_dir = os.path.join(os.path.dirname(__file__), ".anthropic_batches")
init_anthropic_batches(_anthropic_batch_dir)

# 闈欐€佹枃浠剁洰褰?web_dir = Path(__file__).parent / "web"

# 绠＄悊椤甸潰鐢?routes.py 涓殑 router 澶勭悊锛? 鍜?/admin锛?

def main():
    """涓诲嚱鏁?""
    # 鑾峰彇绔彛閰嶇疆
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"""
鈺斺晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晽
鈺?                   Mimo2API Python                       鈺?鈺?         灏嗗皬绫?Mimo AI 杞崲涓?OpenAI 鍏煎 API           鈺?鈺氣晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨暆

馃殌 鏈嶅姟鍣ㄥ惎鍔ㄤ腑...
馃搷 鍦板潃: http://{host}:{port}
馃搳 绠＄悊鐣岄潰: http://{host}:{port}
馃摗 API绔偣: http://{host}:{port}/v1/chat/completions
馃摉 API鏂囨。: http://{host}:{port}/docs

閰嶇疆淇℃伅:
  - API Keys: {len(config_manager.config.api_keys.split(','))} 涓?  - Mimo璐﹀彿: {len(config_manager.config.mimo_accounts)} 涓?
鎸?Ctrl+C 鍋滄鏈嶅姟鍣?""")

    # 鍚姩鏈嶅姟鍣?    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
