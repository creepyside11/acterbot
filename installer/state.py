import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class State:
    def __init__(self, token):
        self.directory = Path(os.environ.get("INSTALLER_STATE", "state"))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "installer.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        key = hmac.digest(token.encode(), b"acter gateway keys v1", hashlib.sha256)
        self.crypto = Fernet(base64.urlsafe_b64encode(key))
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, key TEXT NOT NULL
                );
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    @staticmethod
    def get(db, name):
        row = db.execute("SELECT value FROM config WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def set(db, name, value):
        db.execute("INSERT OR REPLACE INTO config VALUES (?, ?)", (name, str(value)))

    def owner(self):
        with self.db() as db:
            return int(self.get(db, "owner") or 0)

    def issue_claim(self):
        code = secrets.token_urlsafe(18)
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.get(db, "owner"):
                return None
            self.set(db, "claim_hash", digest)
            self.set(db, "claim_until", int(time.time()) + 1800)
        return code

    def claim(self, user, code):
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.get(db, "claim_hash") or ""
            until = int(self.get(db, "claim_until") or 0)
            if self.get(db, "owner") or until < time.time() or not hmac.compare_digest(digest, old):
                return False
            self.set(db, "owner", user)
            db.execute("DELETE FROM config WHERE name IN ('claim_hash', 'claim_until')")
            return True

    def servers(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT id, url FROM servers ORDER BY id")]

    def choose(self, ident):
        with self.db() as db:
            if db.execute("SELECT 1 FROM servers WHERE id = ?", (ident,)).fetchone():
                self.set(db, "selected", ident)
                return True
        return False

    def selected(self):
        with self.db() as db:
            ident = self.get(db, "selected")
            row = db.execute("SELECT url, key FROM servers WHERE id = ?", (ident,)).fetchone()
            if row:
                try:
                    return row["url"], self.crypto.decrypt(row["key"].encode()).decode()
                except InvalidToken:
                    return None
        return None

    def gateway_key(self, url):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT key FROM servers WHERE url = ?", (url,)).fetchone()
            if old:
                try:
                    return self.crypto.decrypt(old["key"].encode()).decode()
                except InvalidToken:
                    pass
            return secrets.token_hex(32)

    def register(self, url, key):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO servers(url,key) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET key=excluded.key",
                (url, self.crypto.encrypt(key.encode()).decode()),
            )
            ident = db.execute("SELECT id FROM servers WHERE url=?", (url,)).fetchone()["id"]
            self.set(db, "selected", ident)
        return ident
