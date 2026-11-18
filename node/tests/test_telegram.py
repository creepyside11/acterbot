import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from aster.config import Config
from aster.store import Store
from aster.telegram import Telegram


class TelegramTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_attachment_and_shared_queue(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "PANEL_PASSWORD": "panel-password-for-telegram-test",
            "DATA_SECRET": "test-data-secret-" * 3,
            "TELEGRAM_BOT_TOKEN": "123456789:ABCDEF12345678912345678",
            "TELEGRAM_OWNER_ID": "42",
            "PUBLIC_URL": "https://panel.example.net",
            "DATABASE_PATH": str(Path(root) / "queue.db"),
            "AI_MODEL": "example-model",
        }):
            config = Config()
            store = Store(config)
            store.initialize()
            requests = []

            async def api(request):
                requests.append(request)
                if request.url.path.endswith("/getFile"):
                    return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/input.txt"}})
                if request.url.path.endswith("/input.txt"):
                    return httpx.Response(200, content=b"Telegram upload")
                return httpx.Response(200, json={"ok": True, "result": True})

            async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
                agent = SimpleNamespace(wake=asyncio.Event())
                bot = Telegram(config, store, agent, client)
                message = {
                    "chat": {"type": "private", "id": 42},
                    "from": {"id": 43},
                    "caption": "Проверь вложение",
                    "document": {"file_id": "sample", "file_name": "notes.txt"},
                }
                await bot.handle({"update_id": 11, "message": message})
                self.assertFalse(requests)
                message["from"]["id"] = 42
                await bot.handle({"update_id": 11, "message": message})
                await bot.handle({"update_id": 11, "message": message})
                self.assertTrue(agent.wake.is_set())
                rows = store.list_jobs()
                self.assertEqual(len(rows), 1)
                task = store.job(rows[0]["id"])
                self.assertEqual(task["prompt"], "Проверь вложение")
                self.assertEqual(store.file(task["files"][0]["id"])["data"], b"Telegram upload")
                response = json.loads(requests[-1].content)
                self.assertEqual(response["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],
                                 "https://panel.example.net/")
