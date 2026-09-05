"""Android 启动入口：在应用私有可写目录下运行 MiMo2API。"""
import os
import sys
import json
import threading
import traceback
from pathlib import Path

_server_thread = None
_state = {"running": False, "port": 8080, "error": None}


def _ensure_config(data_dir: Path):
    """首次运行时从模板生成 config.json 到可写目录。"""
    cfg = data_dir / "config.json"
    if not cfg.exists():
        tpl = Path(__file__).parent / "config.example.json"
        try:
            base = json.loads(tpl.read_text(encoding="utf-8"))
        except Exception:
            base = {}
        base.setdefault("api_keys", "sk-mimo")
        base.setdefault("admin_password", "admin")
        # 剔除模板里的占位账号（YOUR_USER_ID 等），否则会当成真账号显示
        accs = base.get("mimo_accounts") or []
        base["mimo_accounts"] = [
            a for a in accs
            if isinstance(a, dict)
            and a.get("user_id")
            and "YOUR_" not in str(a.get("user_id", "")).upper()
            and "YOUR_" not in str(a.get("service_token", "")).upper()
        ]
        cfg.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def start(data_dir: str, port: int = 8080):
    """由 Java 侧调用。data_dir = context.getFilesDir()。"""
    global _server_thread
    if _state["running"]:
        return "already-running"

    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)

    # 让所有 store 写到可写目录（APK 内的 python 目录是只读的）
    os.environ["MIMO_DATA_DIR"] = str(d)
    cfg = _ensure_config(d)
    os.chdir(str(d))

    _state["port"] = port

    def _run():
        try:
            import uvicorn
            import socket
            import time
            from app.config import config_manager
            try:
                config_manager.config_file = cfg
                config_manager.load()
            except Exception:
                pass
            from main import app as fastapi_app

            # 等端口真正绑定后再标记 running，避免 Java 侧过早跳转
            def _wait_port():
                for _ in range(100):
                    try:
                        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                        s.close()
                        _state["running"] = True
                        print(f"[MiMo2API] 服务已就绪: http://127.0.0.1:{port}")
                        return
                    except (ConnectionRefusedError, OSError):
                        pass
                    time.sleep(0.2)

            import threading
            threading.Thread(target=_wait_port, daemon=True).start()
            uvicorn.run(fastapi_app, host="127.0.0.1", port=port,
                        log_level="info", access_log=False)
        except Exception as e:
            _state["error"] = f"{e}\n{traceback.format_exc()}"
            _state["running"] = False
            print("[MiMo2API] start failed:", _state["error"], file=sys.stderr)

    _server_thread = threading.Thread(target=_run, daemon=True, name="mimo-uvicorn")
    _server_thread.start()
    return "starting"


def status():
    return json.dumps(_state, ensure_ascii=False)
