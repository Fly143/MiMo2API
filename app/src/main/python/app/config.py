"""配置管理模块

敏感字段（serviceToken / userId / xiaomichatbot_ph / admin_password）落盘时
用 Fernet 加密，前缀 enc:v1:。密钥在同目录 .secret_key（已 gitignore）。
旧明文配置可直接加载，下次 save 自动升级为密文。

cryptography 不可用时（如部分 Android/Chaquopy 环境）退化为明文存储并打日志。
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_FERNET = True
except ImportError:  # pragma: no cover - Chaquopy / 无原生依赖环境
    Fernet = None
    InvalidToken = Exception
    HAS_FERNET = False
    print("[Config] cryptography 不可用，敏感字段将以明文写入 config.json")


DEFAULT_API_KEYS = "sk-mimo"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_TOOLS_PASSTHROUGH = False
DEFAULT_COMPRESSION_MODE = "compress"

ENC_PREFIX = "enc:v1:"
_SENSITIVE_ACCOUNT_FIELDS = ("service_token", "user_id", "xiaomichatbot_ph")


class SecretBox:
    """本地密钥 + Fernet 加解密。无 cryptography 时为 no-op（明文）。"""

    def __init__(self, config_path: Path):
        self.key_path = config_path.parent / ".secret_key"
        self._fernet = None
        self._lock = threading.RLock()

    def _load_or_create(self):
        if not HAS_FERNET:
            return None
        with self._lock:
            if self._fernet is not None:
                return self._fernet
            if self.key_path.exists():
                raw = self.key_path.read_bytes().strip()
                self._fernet = Fernet(raw)
                return self._fernet
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            self._fernet = Fernet(key)
            return self._fernet

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None or plaintext == "":
            return ""
        if isinstance(plaintext, str) and plaintext.startswith(ENC_PREFIX):
            return plaintext
        box = self._load_or_create()
        if box is None:
            return plaintext
        token = box.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return ENC_PREFIX + token

    def decrypt(self, value: str) -> str:
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
            return value
        box = self._load_or_create()
        if box is None:
            print("[Config] 无法解密 enc: 数据（缺少 cryptography）")
            return ""
        blob = value[len(ENC_PREFIX):].encode("ascii")
        try:
            return box.decrypt(blob).decode("utf-8")
        except Exception as e:
            print(f"[Config] decrypt failed ({e}); check .secret_key")
            return ""


@dataclass
class MimoAccount:
    """Mimo账号配置"""
    service_token: str
    user_id: str
    xiaomichatbot_ph: str
    login_time: str = ""
    last_test: str = ""
    is_valid: bool = False

    def to_dict(self):
        d = asdict(self)
        d["token_masked"] = self.service_token[:16] + "..." + self.service_token[-6:] if len(self.service_token) > 22 else "***"
        return d

    def to_storage_dict(self, box: SecretBox) -> dict:
        return {
            "service_token": box.encrypt(self.service_token),
            "user_id": box.encrypt(self.user_id),
            "xiaomichatbot_ph": box.encrypt(self.xiaomichatbot_ph),
            "login_time": self.login_time,
            "last_test": self.last_test,
            "is_valid": self.is_valid,
        }


@dataclass
class Config:
    """应用配置"""
    api_keys: str = DEFAULT_API_KEYS
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    mimo_accounts: List[MimoAccount] = None
    models: List[str] = None
    tools_passthrough: bool = DEFAULT_TOOLS_PASSTHROUGH
    compression_mode: str = DEFAULT_COMPRESSION_MODE

    def __post_init__(self):
        if self.mimo_accounts is None:
            self.mimo_accounts = []
        if self.models is None:
            self.models = []

    def to_dict(self):
        d = {
            "api_keys": self.api_keys,
            "admin_password": "***" if self.admin_password else "",
            "mimo_accounts": [acc.to_dict() for acc in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
        }
        if self.models:
            d["models"] = self.models
        return d

    def to_save_dict(self, box: SecretBox):
        d = {
            "api_keys": self.api_keys,
            "admin_password": box.encrypt(self.admin_password),
            "mimo_accounts": [acc.to_storage_dict(box) for acc in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
        }
        if self.models:
            d["models"] = self.models
        return d


def _decrypt_account(raw: dict, box: SecretBox) -> MimoAccount:
    return MimoAccount(
        service_token=box.decrypt(raw.get("service_token", "")),
        user_id=box.decrypt(raw.get("user_id", "")),
        xiaomichatbot_ph=box.decrypt(raw.get("xiaomichatbot_ph", "")),
        login_time=raw.get("login_time", ""),
        last_test=raw.get("last_test", ""),
        is_valid=bool(raw.get("is_valid", False)),
    )


class ConfigManager:
    """配置管理器 - 线程安全"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = Path(config_file)
        self.box = SecretBox(self.config_file)
        self.config = Config()
        self.lock = threading.RLock()
        self.account_idx = 0
        self.load()

    def load(self):
        if not self.config_file.exists():
            self.save()
            return
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            accounts = [
                _decrypt_account(acc, self.box)
                for acc in data.get("mimo_accounts", [])
            ]
            self.config = Config(
                api_keys=data.get("api_keys", DEFAULT_API_KEYS),
                admin_password=self.box.decrypt(data.get("admin_password", DEFAULT_ADMIN_PASSWORD)),
                mimo_accounts=accounts,
                models=data.get("models", []),
                tools_passthrough=data.get("tools_passthrough", DEFAULT_TOOLS_PASSTHROUGH),
                compression_mode=data.get("compression_mode", DEFAULT_COMPRESSION_MODE),
            )
            if self._has_legacy_plaintext(data):
                print("[Config] migrating plaintext secrets to encrypted storage")
                self.save()
        except Exception as e:
            print(f"加载配置失败: {e}")
            self.config = Config()
            self.save()

    @staticmethod
    def _has_legacy_plaintext(data: dict) -> bool:
        if not HAS_FERNET:
            return False
        pw = data.get("admin_password")
        if isinstance(pw, str) and pw and not pw.startswith(ENC_PREFIX):
            return True
        for acc in data.get("mimo_accounts", []):
            for k in _SENSITIVE_ACCOUNT_FIELDS:
                v = acc.get(k)
                if isinstance(v, str) and v and not v.startswith(ENC_PREFIX):
                    return True
        return False

    def save(self):
        with self.lock:
            try:
                payload = self.config.to_save_dict(self.box)
                self.config_file.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                try:
                    os.chmod(self.config_file, 0o600)
                except OSError:
                    pass
            except Exception as e:
                print(f"保存配置失败: {e}")

    def validate_api_key(self, key: str) -> bool:
        with self.lock:
            keys = [k.strip() for k in self.config.api_keys.split(",")]
            return key in keys

    def get_next_account(self) -> Optional[MimoAccount]:
        with self.lock:
            if not self.config.mimo_accounts:
                return None
            account = self.config.mimo_accounts[self.account_idx % len(self.config.mimo_accounts)]
            self.account_idx += 1
            return account

    def update_config(self, new_config: dict):
        """局部更新。敏感字段传入 *** / enc: / 空 时保留原值；未传入的键也保留。"""
        with self.lock:
            if "mimo_accounts" in new_config:
                old_by_uid = {a.user_id: a for a in self.config.mimo_accounts}
                accounts = []
                for acc in new_config.get("mimo_accounts") or []:
                    fields = {k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__}
                    st = fields.get("service_token", "")
                    uid = fields.get("user_id", "")
                    ph = fields.get("xiaomichatbot_ph", "")
                    prev = old_by_uid.get(uid)
                    if prev:
                        if st in ("***", "", None) or (isinstance(st, str) and st.startswith(ENC_PREFIX)):
                            fields["service_token"] = prev.service_token
                        if ph in ("***", "", None) or (isinstance(ph, str) and ph.startswith(ENC_PREFIX)):
                            fields["xiaomichatbot_ph"] = prev.xiaomichatbot_ph
                    accounts.append(MimoAccount(**fields))
            else:
                accounts = self.config.mimo_accounts

            pw = new_config.get("admin_password", self.config.admin_password)
            if pw in ("***", "", None) or (isinstance(pw, str) and pw.startswith(ENC_PREFIX)):
                pw = self.config.admin_password

            self.config = Config(
                api_keys=new_config.get("api_keys", self.config.api_keys),
                admin_password=pw,
                mimo_accounts=accounts,
                models=new_config.get("models", self.config.models),
                tools_passthrough=new_config.get("tools_passthrough", self.config.tools_passthrough),
                compression_mode=new_config.get("compression_mode", self.config.compression_mode),
            )
            self.save()

    def get_config(self) -> dict:
        with self.lock:
            return self.config.to_dict()


config_manager = ConfigManager()
