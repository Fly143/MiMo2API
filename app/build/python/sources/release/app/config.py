"""閰嶇疆绠＄悊妯″潡"""

import json
import threading
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict


@dataclass
class MimoAccount:
    """Mimo璐﹀彿閰嶇疆"""
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


@dataclass
class Config:
    """搴旂敤閰嶇疆"""
    api_keys: str = DEFAULT_API_KEYS
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    mimo_accounts: List[MimoAccount] = None
    models: List[str] = None  # 鑷畾涔夋ā鍨嬪垪琛紝None 琛ㄧず鑷姩鎺㈡祴
    tools_passthrough: bool = DEFAULT_TOOLS_PASSTHROUGH  # 鍏ㄥ眬宸ュ叿閫忎紶妯″紡
    compression_mode: str = DEFAULT_COMPRESSION_MODE  # truncation=瑁佸壀 | compress=LLM鍘嬬缉

    def __post_init__(self):
        if self.mimo_accounts is None:
            self.mimo_accounts = []
        if self.models is None:
            self.models = []

    def to_dict(self):
        d = {
            "api_keys": self.api_keys,
            "admin_password": self.admin_password,
            "mimo_accounts": [acc.to_dict() for acc in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
        }
        if self.models:
            d["models"] = self.models
        return d

    def to_save_dict(self):
        """鐢ㄤ簬淇濆瓨鍒版枃浠剁殑鏍煎紡锛堜笉鍚?token_masked锛?""
        d = {
            "api_keys": self.api_keys,
            "admin_password": self.admin_password,
            "mimo_accounts": [
                {k: v for k, v in acc.to_dict().items() if k != "token_masked"}
                for acc in self.mimo_accounts
            ],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
        }
        if self.models:
            d["models"] = self.models
        return d


class ConfigManager:
    """閰嶇疆绠＄悊鍣?- 绾跨▼瀹夊叏"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = Path(config_file)
        self.config = Config()
        self.lock = threading.RLock()
        self.account_idx = 0
        self.load()

    def load(self):
        """鍔犺浇閰嶇疆"""
        if not self.config_file.exists():
            self.save()
            return
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                accounts = [
                    MimoAccount(**{k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__})
                    for acc in data.get('mimo_accounts', [])
                ]
                self.config = Config(
                    api_keys=data.get('api_keys', DEFAULT_API_KEYS),
                    admin_password=data.get('admin_password', DEFAULT_ADMIN_PASSWORD),
                    mimo_accounts=accounts,
                    models=data.get('models', []),
                    tools_passthrough=data.get('tools_passthrough', DEFAULT_TOOLS_PASSTHROUGH),
                    compression_mode=data.get('compression_mode', DEFAULT_COMPRESSION_MODE)
                )
        except Exception as e:
            print(f"鍔犺浇閰嶇疆澶辫触: {e}")
            self.config = Config()
            self.save()

    def save(self):
        """淇濆瓨閰嶇疆"""
        with self.lock:
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(self.config.to_save_dict(), f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"淇濆瓨閰嶇疆澶辫触: {e}")

    def validate_api_key(self, key: str) -> bool:
        """楠岃瘉API Key"""
        with self.lock:
            keys = [k.strip() for k in self.config.api_keys.split(',')]
            return key in keys

    def get_next_account(self) -> Optional[MimoAccount]:
        """鑾峰彇涓嬩竴涓处鍙凤紙杞锛?""
        with self.lock:
            if not self.config.mimo_accounts:
                return None
            account = self.config.mimo_accounts[self.account_idx % len(self.config.mimo_accounts)]
            self.account_idx += 1
            return account

    def update_config(self, new_config: dict):
        """鏇存柊閰嶇疆"""
        with self.lock:
            accounts = [
                MimoAccount(**{k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__})
                for acc in new_config.get('mimo_accounts', [])
            ]
            self.config = Config(
                api_keys=new_config.get('api_keys', DEFAULT_API_KEYS),
                admin_password=new_config.get('admin_password', DEFAULT_ADMIN_PASSWORD),
                mimo_accounts=accounts,
                models=new_config.get('models', []),
                tools_passthrough=new_config.get('tools_passthrough', DEFAULT_TOOLS_PASSTHROUGH),
                compression_mode=new_config.get('compression_mode', DEFAULT_COMPRESSION_MODE)
            )
            self.save()

    def get_config(self) -> dict:
        """鑾峰彇閰嶇疆"""
        with self.lock:
            return self.config.to_dict()


# 鍏ㄥ眬閰嶇疆绠＄悊鍣ㄥ疄渚?
config_manager = ConfigManager()
