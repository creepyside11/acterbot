import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from aster.server import app


class GatewayTest(unittest.TestCase):
    def test_gateway_dispatches_with_polling_disabled(self):
        key = "test-gateway-" * 4
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "PANEL_PASSWORD": "fixture-panel-secret-for-gateway",
            "DATA_SECRET": "fixture-data-key-for-gateway" * 2,
            "DATABASE_PATH": str(Path(root) / "data.sqlite3"),
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz",
            "TELEGRAM_OWNER_ID": "123", "TELEGRAM_POLL": "0", "GATEWAY_KEY": key,
        }), patch("aster.telegram.Telegram.poll", new_callable=AsyncMock) as polling, patch(
            "aster.telegram.Telegram.handle", new_callable=AsyncMock
        ) as handle, TestClient(app) as client:
            self.assertEqual(client.get("/gateway/status").status_code, 401)
            auth = {"Authorization": "Bearer " + key}
            self.assertEqual(client.get("/gateway/status", headers=auth).json(), {"ready": True, "model": False})
            payload = {"update_id": 456, "message": {"text": "fixture"}}
            self.assertEqual(client.post("/gateway/update", headers=auth, json=payload).json(), {"ok": True})
            handle.assert_awaited_once_with(payload)
            polling.assert_not_called()
