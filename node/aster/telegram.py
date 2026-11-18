import asyncio
import logging
from urllib.parse import quote

import httpx

from .config import MAX_FILE

LOGGER = logging.getLogger(__name__)
STATUSES = {
    "queued": "в очереди", "running": "в работе", "done": "готово",
    "failed": "ошибка", "cancelled": "остановлено",
}


class TelegramError(Exception):
    def __init__(self, method, code):
        self.method = method
        self.code = code
        super().__init__(f"{method}: {code}")


class Telegram:
    def __init__(self, config, store, agent, client):
        self.config = config
        self.store = store
        self.agent = agent
        self.client = client
        self.offset = 0

    async def call(self, method, data, files=None):
        url = "https://api.telegram.org/bot" + self.config.telegram_token + "/" + method
        try:
            parameters = {"data": data, "files": files} if files else {"json": data}
            response = await self.client.post(url, timeout=40, **parameters)
            result = response.json()
            if result.get("ok"):
                return result["result"]
            code = result.get("error_code", response.status_code)
        except (httpx.HTTPError, ValueError, KeyError, AttributeError):
            code = "network"
        raise TelegramError(method, code)

    def panel_keyboard(self):
        if not self.config.public_url.startswith("https://"):
            return None
        button = {"text": "Открыть Aster", "web_app": {"url": self.config.public_url + "/"}}
        return {"inline_keyboard": [[button]]}

    async def send(self, chat, text, panel=False):
        data = {"chat_id": chat, "text": text[:3900], "link_preview_options": {"is_disabled": True}}
        if panel and (keyboard := self.panel_keyboard()):
            data["reply_markup"] = keyboard
        await self.call("sendMessage", data)

    async def get_attachment(self, message):
        document = message.get("document")
        photo = message.get("photo")
        if not document and not photo:
            return []
        item = document or photo[-1]
        if item.get("file_size", 0) > MAX_FILE:
            raise ValueError("Файл должен быть не больше 8 МБ")
        name = item.get("file_name") or "photo.jpg"
        meta = await self.call("getFile", {"file_id": item["file_id"]})
        path = meta.get("file_path")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Telegram не вернул ссылку на вложение")
        url = "https://api.telegram.org/file/bot" + self.config.telegram_token + "/" + quote(path, safe="/")
        data = bytearray()
        try:
            async with self.client.stream("GET", url, timeout=35) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_FILE:
                        raise ValueError("Файл должен быть не больше 8 МБ")
        except httpx.HTTPError:
            raise ValueError("Не удалось скачать вложение из Telegram") from None
        return [(name, bytes(data))]

    async def handle(self, update):
        message = update.get("message") or {}
        chat = message.get("chat", {})
        ident = chat.get("id")
        if chat.get("type") != "private" or message.get("from", {}).get("id") != self.config.telegram_owner:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        first, _, rest = text.partition(" ")
        command = first.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await self.send(
                ident,
                "Напишите задачу обычным сообщением или пришлите файл с подписью.\n\n"
                "/jobs — последние задачи\n/result 12 — ответ и файлы задачи №12\n"
                "/stop 12 — остановить задачу №12\n/panel — открыть Mini App",
                panel=True,
            )
            return
        if command == "/panel":
            await self.send(
                ident, "Откройте панель" if self.config.public_url.startswith("https://")
                else "Укажите публичный HTTPS-адрес в PUBLIC_URL и перезапустите приложение.", panel=True,
            )
            return
        if command == "/jobs":
            jobs = self.store.list_jobs(10)
            lines = [f"#{job['id']} · {STATUSES[job['status']]}\n{job['prompt'][:90]}" for job in jobs]
            await self.send(ident, "\n\n".join(lines) or "Задач пока нет", panel=True)
            return
        if command in {"/result", "/stop"}:
            try:
                if len(rest.strip()) > 18:
                    raise ValueError
                job_id = int(rest.strip().lstrip("#"))
                job = self.store.job(job_id)
                if not job:
                    raise ValueError
            except ValueError:
                await self.send(ident, f"Укажите номер существующей задачи, например {command} 12")
                return
            if command == "/stop":
                changed = self.agent.cancel(job_id)
                await self.send(ident, f"Задача #{job_id} остановлена" if changed else "Задача уже завершилась")
                return
            summary = [f"#{job_id} · {STATUSES[job['status']]}"]
            if job["subagents"]:
                summary.append("\n".join(
                    f"• {item['title']} · {STATUSES[item['status']]}" for item in job["subagents"]
                ))
            result = job["answer"] or job["error"]
            if result:
                summary.append(result)
            elif job["plan"]:
                summary.append(job["plan"][:1450])
            await self.send(ident, "\n\n".join(summary), panel=True)
            for entry in [item for item in job["files"] if item["kind"] == "output"][:8]:
                item = self.store.file(entry["id"])
                await self.call("sendDocument", {"chat_id": str(ident)}, {
                    "document": (item["name"], item["data"], "application/octet-stream")
                })
            if sum(item["kind"] == "output" for item in job["files"]) > 8 or len(job["answer"] or "") > 3800:
                await self.send(ident, "В панели доступен полный результат задачи.", panel=True)
            return
        if command.startswith("/"):
            await self.send(ident, "Доступные команды — /help")
            return
        try:
            if not self.store.model().model:
                raise ValueError("Сначала укажите ID модели в настройках панели")
            uploads = await self.get_attachment(message)
            if not text and not uploads:
                return
            if not text:
                text = "Изучи вложение и кратко опиши его содержимое."
            job = self.store.create_job(text, uploads, chat=ident, source=f"telegram:{update['update_id']}")
        except (ValueError, TelegramError) as error:
            await self.send(ident, str(error) if isinstance(error, ValueError) else "Telegram не смог выдать вложение")
            return
        self.agent.wake.set()
        await self.send(ident, f"Задача #{job} в очереди. Уведомлю о результате.", panel=True)

    async def poll(self):
        if self.config.public_url.startswith("https://"):
            with_timeout = {"menu_button": {"type": "web_app", "text": "Aster", "web_app": {"url": self.config.public_url + "/"}}}
            try:
                await self.call("setChatMenuButton", {"chat_id": self.config.telegram_owner, **with_timeout})
            except TelegramError as error:
                LOGGER.warning("Telegram menu: %s", error)
        while True:
            try:
                updates = await self.call("getUpdates", {"timeout": 27, "offset": self.offset, "allowed_updates": ["message"]})
                for update in updates:
                    try:
                        await self.handle(update)
                    except Exception as error:
                        LOGGER.warning("Telegram update: %s", error if isinstance(error, TelegramError) else type(error).__name__)
                    self.offset = max(self.offset, update["update_id"] + 1)
            except TelegramError as error:
                LOGGER.warning("Telegram polling: %s", error)
                await asyncio.sleep(8)

    async def notify(self):
        while True:
            delay = 2
            for job in self.store.pending_notifications():
                try:
                    status = STATUSES[job["status"]]
                    text = (job["answer"] or job["error"] or "")[:3000]
                    await self.send(job["chat"], f"Задача #{job['id']} · {status}\n\n{text}\n\nПодробнее — /result {job['id']}", panel=True)
                except TelegramError as error:
                    if error.code in (400, 403):
                        self.store.notified(job["id"])
                        continue
                    delay = 20
                    break
                self.store.notified(job["id"])
            await asyncio.sleep(delay)
