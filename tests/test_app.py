import base64
import hmac
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from aster.config import Config
from aster.files import create_file
from aster.server import app
from aster.store import Store

PASSWORD = "test-password-very-long-only-for-local-test"
BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRST_goes_here"


class Provider(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        messages = request["messages"]
        if request["model"] == "slow":
            self.server.gate.wait(10)
        if request.get("tools") and not any(msg["role"] == "tool" for msg in messages):
            message = {
                "content": None,
                "tool_calls": [
                    {"id": "write-1", "type": "function", "function": {
                        "name": "write_file", "arguments": json.dumps({"filename": "итог.txt", "content": "Содержимое результата"}),
                    }},
                ],
            }
        else:
            message = {"content": "OK. Задание выполнено."}
        body = json.dumps({"choices": [{"message": message}]}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.provider.requests = []
        self.provider.gate = threading.Event()
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()

        def stop_provider():
            self.provider.gate.set()
            self.provider.shutdown()
            self.provider.server_close()

        self.addCleanup(stop_provider)
        self.base_url = f"http://127.0.0.1:{self.provider.server_port}/v1"
        self.workspace = Path(self.directory.name) / "workspace"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {
            "PANEL_PASSWORD": PASSWORD, "DATA_SECRET": "test-" * 16,
            "DATABASE_PATH": str(Path(self.directory.name) / "state.db"),
            "ASTER_WORKSPACE": str(self.workspace),
            "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": "", "BROWSER_URL": "",
            "AI_BASE_URL": self.base_url, "AI_MODEL": "test-model", "AI_API_KEY": "test-fixture-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.auth = {"Authorization": "Bearer " + response.json()["token"]}

    def wait(self, job, states):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/jobs/{job}", headers=self.auth)
            self.assertEqual(result.status_code, 200)
            if result.json()["status"] in states:
                return result.json()
            time.sleep(0.05)
        self.fail(f"Задача №{job} осталась в состоянии {result.json()['status']}")

    def update_model(self, **options):
        value = {"base_url": self.base_url, "model": "test-model", **options}
        return self.client.put("/api/settings", headers=self.auth, json=value)

    def test_file_job_and_download(self):
        status = self.client.get("/api/jobs")
        self.assertEqual(status.status_code, 401)
        self.assertIn("frame-ancestors", self.client.get("/").headers["content-security-policy"])
        response = self.client.post("/api/jobs", headers=self.auth, data={"prompt": "Сделай итог"},
                                    files={"files": ("data.txt", "123 из файла".encode(), "text/plain")})
        self.assertEqual(response.status_code, 201, response.text)
        ident = response.json()["id"]
        job = self.wait(ident, {"done", "failed"})
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["answer"], "OK. Задание выполнено.")
        self.assertEqual(len(job["events"]), 6)
        self.assertIn("123 из файла", self.provider.requests[0]["messages"][-1]["content"])
        file = next(item for item in job["files"] if item["kind"] == "output")
        self.assertEqual(file["name"], "итог.txt")
        link = self.client.post(f"/api/files/{file['id']}/link", headers=self.auth).json()["url"]
        download = self.client.get(link)
        self.assertEqual(download.content.decode(), "Содержимое результата")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(self.client.get(link[:-2]).status_code, 401)
        retry = self.client.post(f"/api/jobs/{ident}/retry", headers=self.auth)
        self.assertEqual(retry.status_code, 201)
        repeat = self.wait(retry.json()["id"], {"done", "failed"})
        self.assertTrue(any(item["kind"] == "input" for item in repeat["files"]))

    def test_filter_and_search_job_history(self):
        store = Store(Config())
        done = store.create_job("Проверка файла")
        store.finish(done, "done", answer="В готовом ОТЧЁТЕ есть таблица")
        cancelled = store.create_job("Черновик")
        store.finish(cancelled, "cancelled")
        self.assertEqual(
            [row["id"] for row in self.client.get("/api/jobs", headers=self.auth, params={
                "q": "отчёте", "status": "done", "before": cancelled,
            }).json()],
            [done],
        )
        self.assertEqual(self.client.get("/api/jobs", headers=self.auth, params={"q": f"#{cancelled}"}).json()[0]["id"], cancelled)
        self.assertEqual(self.client.get("/api/jobs", headers=self.auth, params={"q": "%"}).json(), [])
        self.assertEqual(self.client.get("/api/jobs/9223372036854775808", headers=self.auth).status_code, 422)

    def test_workspace_browser_access_and_upload_conflicts(self):
        folder = self.workspace / "Материалы"
        folder.mkdir()
        outside = self.workspace.parent / "outside.txt"
        outside.write_bytes(b"private fixture")
        (folder / "link.txt").symlink_to(outside)
        (folder / "bad\nname.txt").write_bytes(b"invalid path")
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        params = {"path": "Материалы"}
        files = {"file": ("отчёт.txt", b"workspace fixture", "text/plain")}
        response = self.client.post("/api/workspace/upload", headers=self.auth, data=params, files=files)
        self.assertEqual(response.status_code, 201, response.text)
        path = response.json()["path"]
        self.assertEqual(
            self.client.get("/api/workspace", headers=self.auth, params=params).json()["entries"],
            [{"path": path, "type": "file", "bytes": 17}],
        )
        link = self.client.post("/api/workspace/link", headers=self.auth, params={"path": path}).json()["url"]
        download = self.client.get(link)
        self.assertEqual(download.content, b"workspace fixture")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(self.client.get(link[:-2]).status_code, 401)
        collision = self.client.post(
            "/api/workspace/upload", headers=self.auth, data=params, files={"file": ("отчёт.txt", b"overwrite")},
        )
        self.assertEqual(collision.status_code, 409)
        for forbidden in ("../outside.txt", "Материалы/link.txt"):
            self.assertEqual(self.client.post("/api/workspace/link", headers=self.auth, params={"path": forbidden}).status_code, 400)
        unsafe_upload = self.client.post("/api/workspace/upload", headers=self.auth, data={"path": ".."}, files=files)
        self.assertEqual(unsafe_upload.status_code, 400)
        self.assertEqual(outside.read_bytes(), b"private fixture")

    def test_settings_and_vision(self):
        result = self.update_model(vision=True, tools=False)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["has_key"])
        self.assertNotIn("api_key", result.json())
        self.assertNotIn(b"test-fixture-key", Path(os.environ["DATABASE_PATH"]).read_bytes())
        probe = self.client.post("/api/settings/test", headers=self.auth)
        self.assertEqual(probe.status_code, 200, probe.text)
        picture = io.BytesIO()
        Image.new("RGB", (12, 12), "blue").save(picture, format="PNG")
        response = self.client.post(
            "/api/jobs", headers=self.auth, data={"prompt": "Что на фото?"},
            files={"files": ("photo.png", picture.getvalue(), "image/png")},
        )
        job = self.wait(response.json()["id"], {"done", "failed"})
        self.assertEqual(job["status"], "done", job)
        frame = self.provider.requests[-1]["messages"][-1]["content"][1]["image_url"]["url"]
        self.assertTrue(base64.b64decode(frame.split(",", 1)[1]).startswith(b"\xff\xd8"))
        saved = self.update_model(api_key="")
        self.assertFalse(saved.json()["has_key"])

    def test_running_and_queued_cancellation(self):
        self.update_model(model="slow")
        first = self.client.post("/api/jobs", headers=self.auth, data={"prompt": "Долгая задача"}).json()["id"]
        self.wait(first, {"running"})
        second = self.client.post("/api/jobs", headers=self.auth, data={"prompt": "Следующая задача"}).json()["id"]
        self.assertEqual(self.wait(second, {"queued"})["status"], "queued")
        for job in (second, first):
            self.assertEqual(self.client.post(f"/api/jobs/{job}/cancel", headers=self.auth).status_code, 200)
            self.wait(job, {"cancelled"})
        self.provider.gate.set()
        self.update_model()
        third = self.client.post("/api/jobs", headers=self.auth, data={"prompt": "Проверка после отмены"}).json()["id"]
        self.assertEqual(self.wait(third, {"done", "failed"})["status"], "done")

    def test_restart_preserves_queued_jobs(self):
        store = Store(Config())
        first = store.create_job("Запущена")
        second = store.create_job("В очереди")
        store.claim()
        store.initialize()
        self.assertEqual(store.job(first)["status"], "failed")
        self.assertEqual(store.job(second)["status"], "queued")


class AuthenticationAndFilesTest(unittest.TestCase):
    def test_telegram_hmac(self):
        env = {"PANEL_PASSWORD": PASSWORD, "DATA_SECRET": "d" * 48,
               "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_OWNER_ID": "42"}
        with patch.dict(os.environ, env):
            cfg = Config()
        fields = {"auth_date": str(int(time.time())), "user": '{"id":42}', "signature": "test", "query_id": "abc"}
        key = hmac.digest(b"WebAppData", BOT_TOKEN.encode(), "sha256")
        signature = hmac.digest(key, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), "sha256").hex()
        data = urlencode({**fields, "hash": signature})
        self.assertTrue(cfg.valid_telegram(data))
        self.assertFalse(cfg.valid_telegram(data.replace("42", "43")))
        self.assertTrue(cfg.valid_session(cfg.session()))
        self.assertFalse(cfg.valid_session("100.rejected"))

    def test_excel_keeps_provided_cells_as_text(self):
        created = create_file("table.xlsx", "Name,Data\nExample,=1+2\n")
        book = load_workbook(io.BytesIO(created), read_only=True)
        with self.add_book_close(book):
            cell = book.active["B2"]
            self.assertEqual((cell.value, cell.data_type), ("=1+2", "s"))

    @staticmethod
    def add_book_close(book):
        from contextlib import closing

        return closing(book)


if __name__ == "__main__":
    unittest.main()
