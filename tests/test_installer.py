import asyncio
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from installer import ssh
from installer.bot import InstallerBot
from installer.state import State

TOKEN = "123456789:tester-for-installer-only-no-secret"


class Api:
    token = TOKEN

    def __init__(self):
        self.events = []

    async def send(self, chat, text, keyboard=None):
        self.events.append(("send", text))

    async def erase(self, chat, message_id):
        self.events.append(("erase", message_id))


def update(text, user=123, kind="private", ident=10):
    return {"update_id": ident, "message": {
        "chat": {"id": user, "type": kind}, "from": {"id": user}, "message_id": ident, "text": text,
    }}


class BotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env = patch.dict(os.environ, {"INSTALLER_STATE": temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = State(TOKEN)
        self.api = Api()

    async def test_owner_binding_and_load_erase_before_ssh(self):
        code = self.state.issue_claim()
        requests = []
        async with httpx.AsyncClient(trust_env=False) as client:
            bot = InstallerBot(self.api, self.state, client)
            await bot.handle(update("/claim " + code, user=876, kind="group"))
            self.assertEqual(self.state.owner(), 0)
            await bot.handle(update("/claim " + code))
            self.assertEqual(self.state.owner(), 123)
            await bot.handle(update("/load", user=500))
            self.assertIsNone(bot.form)
            await bot.handle(update("/load"))
            await bot.handle(update("8.8.8.8 2222"))
            await bot.handle(update("root"))
            await bot.handle(update("SHA256:" + "A" * 43))

            def connect(target, login, password, expected):
                from contextlib import nullcontext
                self.assertIn(("erase", 25), self.api.events)
                requests.append((target.address, target.port, login, password))
                return nullcontext(object())

            with patch("installer.ssh.connection", connect), patch("installer.ssh.metrics", return_value="CPU — 1.0%"):
                await bot.handle(update("fake quote ' ; password", ident=25))
                await asyncio.wait_for(bot.work, timeout=2)
            self.assertEqual(requests, [("8.8.8.8", 2222, "root", "fake quote ' ; password")])
            self.assertIn("CPU — 1.0%", " ".join(text for event, text in self.api.events if event == "send"))
            self.assertNotIn(b"fake quote", self.state.path.read_bytes())
            self.assertFalse(self.state.claim(500, code))

    async def test_forward_task_to_selected_gateway(self):
        self.state.claim(123, self.state.issue_claim())
        key = self.state.gateway_key("https://8.8.8.8")
        self.state.register("https://8.8.8.8", key)
        requests = []

        def remote(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(remote), trust_env=False) as client:
            bot = InstallerBot(self.api, self.state, client)
            await bot.handle(update("/ask подготовь отчёт"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://8.8.8.8/gateway/update")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer " + key)
        import json
        self.assertEqual(json.loads(requests[0].content)["message"]["text"], "подготовь отчёт")
        self.assertNotIn(key.encode(), self.state.path.read_bytes())


class RemoteTest(unittest.TestCase):
    def test_target_limits_and_payload(self):
        for value in ("127.0.0.1", "192.168.0.1", "169.254.169.254", "224.0.0.1", "8.8.8.8 0", "example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ssh.Target.parse(value)
        target = ssh.Target.parse("2606:4700:4700::1111")
        self.assertEqual(target.url, "https://[2606:4700:4700::1111]")
        config = ssh.node_env(target, TOKEN, 123, "b" * 64).decode()
        self.assertIn("TELEGRAM_POLL=0", config)
        self.assertIn("TELEGRAM_OWNER_ID=123", config)
        with tarfile.open(fileobj=io.BytesIO(ssh.node_archive())) as tar:
            names = set(tar.getnames())
            self.assertIn("compose.yaml", names)
            self.assertIn("aster/static/index.html", names)
            self.assertNotIn(".env", names)

    def test_metrics_and_bootstrap_upgrade(self):
        with patch("installer.ssh.run_command", side_effect=lambda transport, command, seconds:
                   subprocess.check_output(command, shell=True, text=True)):
            self.assertIn("CPU", ssh.metrics(object()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "upload"
            incoming.mkdir()
            (incoming / "node.tar.gz").write_bytes(ssh.node_archive())
            bootstrap = root / "bootstrap.sh"
            bootstrap.write_text((ssh.HERE / "bootstrap.sh").read_text().replace("/opt/acter-aster", str(root / "install")))
            fakebin = root / "bin"
            fakebin.mkdir()
            for name, text in {
                "id": "#!/bin/sh\necho 0\n",
                "docker": "#!/bin/sh\nif [ \"$1\" = compose ] && [ \"$*\" != 'compose version' ]; then\n"
                          "  if [ -f .env ] && grep -q '^TELEGRAM_OWNER_ID=999$' .env; then exit 1; fi\n"
                          "fi\nexit 0\n",
            }.items():
                script = fakebin / name
                script.write_text(text)
                script.chmod(0o755)
            env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}
            command = ["bash", str(bootstrap), str(incoming)]
            current = root / "install/current/.env"
            for owner in (123, 456):
                (incoming / "node.env").write_bytes(ssh.node_env(ssh.Target.parse("8.8.8.8"), TOKEN, owner, "a" * 64))
                subprocess.run(command, env=env, check=True, capture_output=True)
                if owner == 123:
                    password = current.read_text().splitlines()[0]
            self.assertIn(password, current.read_text())
            self.assertIn("TELEGRAM_OWNER_ID=456", current.read_text())
            (incoming / "node.env").write_bytes(ssh.node_env(ssh.Target.parse("8.8.8.8"), TOKEN, 999, "c" * 64))
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b"ACTER_STAGE=rollback", failed.stdout)
            self.assertIn("TELEGRAM_OWNER_ID=456", current.read_text())
