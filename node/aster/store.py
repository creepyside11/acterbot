import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from .config import ModelConfig

ACTIVE = ("queued", "running")
MAX_OUTPUTS = 20
MAX_JOB_TEXT = 16_000
MAX_ACTIVE = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created TEXT NOT NULL,
    started TEXT,
    ended TEXT,
    answer TEXT,
    error TEXT,
    parent INTEGER REFERENCES jobs(id),
    chat INTEGER,
    source TEXT UNIQUE,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    time TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job, id);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS files_job ON files(job);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_progress (
    job INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subagents (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    answer TEXT,
    state TEXT,
    PRIMARY KEY (job, ref)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name):
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[^\w .()+-]', '_', name).strip(" .")[:120]
    return name or "file.txt"


class Store:
    def __init__(self, config):
        self.config = config

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.config.database, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db

    def initialize(self):
        self.config.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            rows = db.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
            for row in rows:
                if db.execute("SELECT 1 FROM job_progress WHERE job = ?", (row["id"],)).fetchone():
                    db.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (row["id"],))
                    db.execute("UPDATE subagents SET status = 'queued' WHERE job = ? AND status = 'running'", (row["id"],))
                    self.log(db, row["id"], "Сервер перезапустился. Задача продолжится с сохранённого шага")
                    continue
                message = "Сервер перезапустился. Задачу можно запустить заново."
                db.execute("UPDATE jobs SET status = 'failed', ended = ?, error = ? WHERE id = ?", (now(), message, row["id"]))
                self.log(db, row["id"], message)

    def model(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name = 'model'").fetchone()
        if row is None:
            return self.config.default_model
        return ModelConfig.model_validate_json(self.config.fernet.decrypt(row["value"].encode()))

    def set_model(self, model):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if model.api_key is None:
                row = db.execute("SELECT value FROM settings WHERE name = 'model'").fetchone()
                old = ModelConfig.model_validate_json(self.config.fernet.decrypt(row["value"].encode())) if row else self.config.default_model
                if old.base_url == model.base_url:
                    model = model.model_copy(update={"api_key": old.api_key})
            value = self.config.fernet.encrypt(model.model_dump_json().encode()).decode()
            db.execute("INSERT OR REPLACE INTO settings VALUES ('model', ?)", (value,))
        return model

    @staticmethod
    def log(db, job, text):
        db.execute("INSERT INTO events(job, time, text) VALUES (?, ?, ?)", (job, now(), text[:400]))

    def event(self, job, text):
        with self.connect() as db:
            self.log(db, job, text)

    @staticmethod
    def insert_file(db, job, name, kind, data, ident=None):
        ident = ident or uuid.uuid4().hex
        db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?)", (ident, job, safe_name(name), kind, data))
        return ident

    def create_job(self, prompt, uploads=(), parent=None, chat=None, source=None, retry=None):
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= MAX_JOB_TEXT:
            raise ValueError("Задание должно содержать от 1 до 16 000 символов")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source:
                previous = db.execute("SELECT id FROM jobs WHERE source = ?", (source,)).fetchone()
                if previous:
                    return previous["id"]
            if parent is not None and not db.execute("SELECT 1 FROM jobs WHERE id = ?", (parent,)).fetchone():
                raise LookupError
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= MAX_ACTIVE:
                raise ValueError("Очередь заполнена")
            job = db.execute(
                "INSERT INTO jobs(prompt, created, parent, chat, source) VALUES (?, ?, ?, ?, ?)",
                (prompt, now(), parent, chat, source),
            ).lastrowid
            if retry is not None:
                db.execute(
                    "INSERT INTO files SELECT lower(hex(randomblob(16))), ?, name, kind, data FROM files WHERE job = ? AND kind = 'input'",
                    (job, retry),
                )
            for name, data in uploads:
                self.insert_file(db, job, name, "input", data)
            self.log(db, job, "Задача добавлена в очередь")
            return job

    def list_jobs(self, limit=80, before=None):
        with self.connect() as db:
            rows = db.execute(
                """SELECT id, substr(prompt, 1, 240) AS prompt, status, created, started, ended
                   FROM jobs WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?""",
                (before, before, limit),
            )
            return [dict(row) for row in rows]

    def job(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["events"] = [dict(event) for event in db.execute(
                "SELECT time, text FROM events WHERE job = ? ORDER BY id", (ident,)
            )]
            item["files"] = [dict(file) for file in db.execute(
                "SELECT id, name, kind, length(data) AS bytes FROM files WHERE job = ? ORDER BY rowid", (ident,)
            )]
            progress = db.execute("SELECT state FROM job_progress WHERE job = ?", (ident,)).fetchone()
            item["plan"] = json.loads(progress["state"]).get("plan", "") if progress else ""
            item["subagents"] = [dict(worker) for worker in db.execute(
                "SELECT ref, title, prompt, status, answer FROM subagents WHERE job = ? ORDER BY rowid", (ident,)
            )]
            return item

    def previous_context(self, job):
        context = []
        parent = job["parent"]
        with self.connect() as db:
            while parent is not None and len(context) < 3:
                row = db.execute("SELECT prompt, answer, parent FROM jobs WHERE id = ?", (parent,)).fetchone()
                if row is None:
                    break
                context.append((row["prompt"][:3500], (row["answer"] or "")[:4500]))
                parent = row["parent"]
        return list(reversed(context))

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row is not None:
                db.execute("UPDATE jobs SET status = 'running', started = coalesce(started, ?) WHERE id = ?", (now(), row["id"]))
                self.log(db, row["id"], "Агент продолжил работу" if row["started"] else "Агент начал работу")
                return dict(row)
        return None

    def finish(self, job, status, answer=None, error=None):
        with self.connect() as db:
            changed = db.execute(
                """UPDATE jobs SET status = ?, answer = ?, error = ?, ended = ?
                   WHERE id = ? AND status IN ('queued', 'running')""",
                (status, answer, error, now(), job),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE subagents SET status = 'cancelled', state = NULL WHERE job = ? AND status IN ('queued', 'running')",
                    (job,),
                )
                row = db.execute("SELECT state FROM job_progress WHERE job = ?", (job,)).fetchone()
                if row:
                    state = {"plan": json.loads(row["state"]).get("plan", "")}
                    db.execute("UPDATE job_progress SET state = ? WHERE job = ?", (json.dumps(state, ensure_ascii=False), job))
                self.log(db, job, error or {"done": "Задача завершена", "cancelled": "Задача остановлена"}.get(status, status))
        return bool(changed)

    def progress(self, job):
        with self.connect() as db:
            row = db.execute("SELECT state FROM job_progress WHERE job = ?", (job,)).fetchone()
            return json.loads(row["state"]) if row else None

    def checkpoint(self, job, state, subagent=None):
        encoded = json.dumps(state, ensure_ascii=False)
        with self.connect() as db:
            if subagent is None:
                db.execute("INSERT OR REPLACE INTO job_progress VALUES (?, ?)", (job, encoded))
            else:
                db.execute(
                    "UPDATE subagents SET state = ? WHERE job = ? AND ref = ? AND status = 'running'",
                    (encoded, job, subagent),
                )

    def delegate(self, job, batch, tasks, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prefix = batch + ":"
            children = db.execute(
                "SELECT * FROM subagents WHERE job = ? AND ref GLOB ? ORDER BY rowid", (job, prefix + "*")
            ).fetchall()
            if not children:
                count = db.execute("SELECT count(*) FROM subagents WHERE job = ?", (job,)).fetchone()[0]
                if count + len(tasks) > limit:
                    raise ValueError(f"Можно запустить ещё {max(0, limit - count)} субагентов в этой задаче")
                db.executemany(
                    "INSERT INTO subagents(job, ref, title, prompt) VALUES (?, ?, ?, ?)",
                    [(job, prefix + str(index), task["title"], task["task"]) for index, task in enumerate(tasks)],
                )
                children = db.execute(
                    "SELECT * FROM subagents WHERE job = ? AND ref GLOB ? ORDER BY rowid", (job, prefix + "*")
                ).fetchall()
            return [dict(child) for child in children]

    def start_subagent(self, job, ref, title):
        with self.connect() as db:
            db.execute("UPDATE subagents SET status = 'running' WHERE job = ? AND ref = ? AND status = 'queued'", (job, ref))
            self.log(db, job, f"Субагент начал работу · {title}")

    def finish_subagent(self, job, ref, title, status, answer):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE subagents SET status = ?, answer = ?, state = NULL WHERE job = ? AND ref = ? AND status = 'running'",
                (status, answer, job, ref),
            ).rowcount
            if changed:
                label = "Субагент завершил работу" if status == "done" else "Ошибка субагента"
                self.log(db, job, f"{label} · {title}")

    def pending_notifications(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT id, chat, status, answer, error FROM jobs WHERE chat IS NOT NULL
                   AND notified = 0 AND status NOT IN ('queued', 'running') ORDER BY id LIMIT 5"""
            )]

    def notified(self, job):
        with self.connect() as db:
            db.execute("UPDATE jobs SET notified = 1 WHERE id = ?", (job,))

    def workspace_files(self, job):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, name, kind, length(data) AS bytes FROM files WHERE job = ? ORDER BY rowid", (job,)
            )]

    def file(self, ident, job=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE id = ? AND (? IS NULL OR job = ?)", (ident, job, job)).fetchone()
            return dict(row) if row else None

    def output(self, job, name, data, action=None):
        ident = uuid.uuid5(uuid.NAMESPACE_URL, f"aster:{job}:{action}").hex if action else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if ident and db.execute("SELECT 1 FROM files WHERE id = ?", (ident,)).fetchone():
                return ident
            count = db.execute("SELECT count(*) FROM files WHERE job = ? AND kind = 'output'", (job,)).fetchone()[0]
            if count >= MAX_OUTPUTS:
                raise ValueError("Достигнут лимит файлов результата")
            return self.insert_file(db, job, name, "output", data, ident)
