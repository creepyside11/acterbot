import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, field_validator

MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 4
MAX_BODY = MAX_FILE * MAX_FILES + 128 * 1024
BOT_TOKEN_PATTERN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}")


class ModelConfig(BaseModel):
    base_url: str = Field(default="https://api.openai.com/v1", max_length=300)
    model: str = Field(default="", max_length=150)
    api_key: str | None = Field(default=None, max_length=1000)
    vision: bool = False
    tools: bool = True
    steps: int = Field(default=24, ge=1, le=250)
    minutes: int = Field(default=30, ge=1, le=600)
    subagents: int = Field(default=3, ge=0, le=3)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value):
        value = value.strip().rstrip("/")
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"} or not url.hostname or url.username
            or url.password or url.query or url.fragment or any(c.isspace() for c in value)
        ):
            raise ValueError("Укажите Base URL API, например https://api.openai.com/v1")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}:
            raise ValueError("Внешний API должен использовать HTTPS")
        url.port
        return value.removesuffix("/chat/completions")

    @field_validator("model", "api_key")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if value is not None else None


class Config:
    def __init__(self):
        self.password = os.environ.get("PANEL_PASSWORD", "")
        secret = os.environ.get("DATA_SECRET", "")
        if len(self.password) < 24 or len(secret) < 32:
            raise RuntimeError("Создайте .env через python3 setup.py")
        self.signing = hmac.digest(secret.encode(), b"panel-session:" + self.password.encode(), "sha256")
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        self.database = Path(os.environ.get("DATABASE_PATH", "data/aster.sqlite3"))
        workspace = os.environ.get("ASTER_WORKSPACE", "")
        self.workspace = Path(workspace) if workspace else None
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_owner = int(os.environ.get("TELEGRAM_OWNER_ID") or "0")
        self.telegram_poll = os.environ.get("TELEGRAM_POLL", "1") == "1"
        self.gateway_key = os.environ.get("GATEWAY_KEY", "")
        if self.gateway_key and (len(self.gateway_key) < 32 or self.telegram_poll or not self.telegram_token):
            raise RuntimeError("Для шлюза настройте токен и TELEGRAM_POLL=0")
        if self.telegram_token and (not BOT_TOKEN_PATTERN.fullmatch(self.telegram_token) or self.telegram_owner <= 0):
            raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID в .env")
        self.public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        self.browser_url = os.environ.get("BROWSER_URL", "").rstrip("/")
        self.browser_token = os.environ.get("BROWSER_TOKEN", "")
        if self.browser_url and len(self.browser_token) < 32:
            raise RuntimeError("BROWSER_TOKEN должен содержать не менее 32 символов")
        self.default_model = ModelConfig(
            base_url=os.environ.get("AI_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("AI_MODEL", ""),
            api_key=os.environ.get("AI_API_KEY"),
            vision=os.environ.get("AI_VISION") == "1",
        )

    def session(self):
        expires = str(int(time.time()) + 12 * 3600)
        signature = hmac.digest(self.signing, expires.encode(), "sha256").hex()
        return expires + "." + signature

    def valid_session(self, token):
        expires, _, signature = token.partition(".")
        if not expires.isascii() or not expires.isdecimal() or len(expires) > 11 or int(expires) < time.time():
            return False
        expected = hmac.digest(self.signing, expires.encode(), "sha256").hex()
        return hmac.compare_digest(expected.encode(), signature.encode())

    def valid_telegram(self, data):
        if not self.telegram_token or not self.telegram_owner:
            return False
        try:
            pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
            fields = dict(pairs)
            if len(fields) != len(pairs):
                return False
            supplied = fields.pop("hash")
            check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
            key = hmac.digest(b"WebAppData", self.telegram_token.encode(), "sha256")
            expected = hmac.digest(key, check.encode(), "sha256").hex()
            if not hmac.compare_digest(expected.encode(), supplied.encode()):
                return False
            age = time.time() - int(fields["auth_date"])
            user = json.loads(fields["user"])
            return -60 <= age <= 3600 and isinstance(user, dict) and user.get("id") == self.telegram_owner
        except (KeyError, TypeError, ValueError):
            return False
