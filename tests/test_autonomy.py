import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aster.server import app
from aster.workspace import Workspace

PASSWORD = "fixture-password-for-autonomy-tests"


def call(ident, name, **args):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class AutonomyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("Три идеи и два вопроса", encoding="utf-8")
        env = patch.dict(os.environ, {
            "PANEL_PASSWORD": PASSWORD, "DATA_SECRET": "fixture-secret-" * 4,
            "DATABASE_PATH": str(self.root / "state.db"), "ASTER_WORKSPACE": str(self.workspace),
            "AI_MODEL": "fixture-model", "TELEGRAM_BOT_TOKEN": "", "BROWSER_URL": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def headers(self, client):
        result = client.post("/api/login", json={"password": PASSWORD})
        return {"Authorization": "Bearer " + result.json()["token"]}

    def start(self, client):
        auth = self.headers(client)
        response = client.post("/api/jobs", headers=auth, data={"prompt": "Раздели и подготовь отчёт"})
        self.assertEqual(response.status_code, 201, response.text)
        return auth, response.json()["id"]

    def wait(self, client, auth, job, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job}", headers=auth)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            if predicate(data):
                return data
            time.sleep(0.03)
        self.fail(f"Задача №{job} не достигла ожидаемого состояния: {data}")

    def test_resume_reuses_subagent_and_saves_workspace_report(self):
        turns = {"Идеи": 0, "Вопросы": 0}
        resumed = False

        async def fake_completion(client, model, messages, tools=()):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            results = {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"}
            names = {item["function"]["name"] for item in tools}
            if prompt in turns:
                turns[prompt] += 1
                assert "delegate" not in names and "workspace_write" not in names
                if prompt == "Вопросы" and not resumed:
                    await asyncio.sleep(60)
                if "read" not in results:
                    return {"tool_calls": [call("read", "workspace_read", path="notes.txt", offset=0)]}
                assert "Три идеи" in results["read"]
                return {"content": f"{prompt}: заметки изучены"}
            assert {"delegate", "workspace_write", "workspace_export"} <= names
            if "team" not in results:
                return {"tool_calls": [
                    call("plan", "update_plan", plan="1. Раздать подзадачи\n2. Сохранить отчёт"),
                    call("team", "delegate", tasks=[
                        {"title": "Главные идеи", "task": "Идеи"}, {"title": "Открытые вопросы", "task": "Вопросы"},
                    ]),
                ]}
            assert "Идеи: заметки изучены" in results["team"] and "Вопросы: заметки изучены" in results["team"]
            if "export" not in results:
                return {"tool_calls": [
                    call("save", "workspace_write", path="reports/result.md", content="# Итог\nДва результата", expected_sha256=""),
                    call("export", "workspace_export", path="reports/result.md"),
                ]}
            assert "result.md" in results["export"]
            return {"content": "Сохранил итог в reports/result.md"}

        with patch("aster.agent.completion", fake_completion):
            with TestClient(app) as client:
                auth, job = self.start(client)
                self.wait(client, auth, job, lambda item: [r["status"] for r in item["subagents"]] == ["done", "running"])
                self.assertEqual(turns, {"Идеи": 2, "Вопросы": 1})
            resumed = True
            with TestClient(app) as client:
                auth = self.headers(client)
                data = self.wait(client, auth, job, lambda item: item["status"] in {"done", "failed"})
                self.assertEqual(data["status"], "done", data)
                self.assertIn("Раздать подзадачи", data["plan"])
                self.assertEqual([r["status"] for r in data["subagents"]], ["done", "done"])
                self.assertEqual(turns, {"Идеи": 2, "Вопросы": 3})
                self.assertTrue(any("продолжится" in row["text"] for row in data["events"]))
                self.assertEqual((self.workspace / "reports/result.md").read_text(), "# Итог\nДва результата")
                self.assertEqual([file["name"] for file in data["files"]], ["result.md"])

    def test_cancel_parallel_children(self):
        cancelled = set()

        async def fake_completion(client, model, messages, tools=()):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if prompt in {"А", "Б"}:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.add(prompt)
                    raise
            return {"tool_calls": [call("group", "delegate", tasks=[
                {"title": "Первый", "task": "А"}, {"title": "Второй", "task": "Б"},
            ])]}

        with patch("aster.agent.completion", fake_completion), TestClient(app) as client:
            auth, job = self.start(client)
            self.wait(client, auth, job, lambda item: [r["status"] for r in item["subagents"]] == ["running", "running"])
            self.assertEqual(client.post(f"/api/jobs/{job}/cancel", headers=auth).status_code, 200)
            data = self.wait(client, auth, job, lambda item: item["status"] == "cancelled")
            self.assertEqual([r["status"] for r in data["subagents"]], ["cancelled", "cancelled"])
            deadline = time.monotonic() + 3
            while len(cancelled) != 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(cancelled, {"А", "Б"})


class WorkspaceTest(unittest.TestCase):
    def test_paths_and_versioned_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "workspace"
            folder.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("private fixture")
            (folder / "link.txt").symlink_to(outside)
            (folder / "external").symlink_to(Path(tmp), target_is_directory=True)
            os.link(outside, folder / "hardlink.txt")
            (folder / ".hidden.txt").write_text("hidden fixture")
            workspace = Workspace(folder)
            data = "Ж" * 11_000
            original = workspace.write("reports/отчёт.txt", data.encode(), "")
            self.assertEqual(workspace.write("reports/отчёт.txt", data.encode(), ""), original)
            self.assertEqual(workspace.read_page("reports/отчёт.txt", 0)["next_offset"], 10_000)
            self.assertEqual(workspace.read_page("reports/отчёт.txt", 10_000)["text"], "Ж" * 1000)
            self.assertEqual(workspace.list(".")["entries"], [{"path": "reports", "type": "directory"}])
            for path in ("../outside.txt", "/etc/passwd", "link.txt", "external/outside.txt", "hardlink.txt", ".hidden.txt"):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        workspace.read(path)
                    with self.assertRaises(ValueError):
                        workspace.write(path, b"sample", "")
            with self.assertRaises(ValueError):
                workspace.write("reports/отчёт.txt", b"new", "")
            workspace.write("reports/отчёт.txt", b"new", original["sha256"])
            with self.assertRaises(ValueError):
                workspace.write("reports/отчёт.txt", b"another", original["sha256"])
            self.assertEqual((folder / "reports/отчёт.txt").read_bytes(), b"new")
            self.assertEqual(outside.read_text(), "private fixture")
