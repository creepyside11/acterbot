import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field

import httpx

from . import ssh

KEYBOARD = {
    "keyboard": [[{"text": "/install"}, {"text": "/load"}], [{"text": "/panel"}, {"text": "/servers"}]],
    "resize_keyboard": True,
}
HELP = (
    "/install — развернуть Aster на VPS\n"
    "/load — проверить CPU, память и диск по SSH\n"
    "/panel — открыть веб-панель выбранного VPS\n"
    "/servers — установленные VPS\n"
    "/use 1 — выбрать VPS по номеру\n"
    "/ask текст — отправить задачу Aster\n"
    "/jobs — список задач\n"
    "/result 1 — результат и файлы\n"
    "/stop 1 — остановить задачу Aster\n"
    "/cancel — выйти из ввода данных"
)
PROMPTS = (
    "Введите публичный IP VPS. Для нестандартного порта укажите IP и порт через пробел.",
    "Введите имя пользователя SSH, например root или ubuntu.",
    "Введите отпечаток хост-ключа SSH Ed25519 в формате SHA256:…. Его можно узнать в консоли хостинга командой:\n"
    "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
    "Отправьте пароль SSH одним сообщением. Установщик удалит сообщение перед подключением. "
    "Ввод работает только в этом личном чате.",
)


class TelegramUnavailable(Exception):
    pass


class TelegramAPI:
    def __init__(self, token, client):
        self.token = token
        self.client = client

    async def call(self, method, data):
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=data, timeout=42
            )
            body = response.json()
            if body.get("ok") and response.is_success:
                return body["result"]
            raise TelegramUnavailable(f"Telegram {method}, HTTP {response.status_code}")
        except (KeyError, ValueError, AttributeError, httpx.HTTPError):
            raise TelegramUnavailable(f"Telegram {method} недоступен") from None

    async def send(self, chat, text, keyboard=None):
        payload = {"chat_id": chat, "text": text,
                   "link_preview_options": {"is_disabled": True}}
        if keyboard:
            payload["reply_markup"] = keyboard
        await self.call("sendMessage", payload)

    async def erase(self, chat, message_id):
        await self.call("deleteMessage", {"chat_id": chat, "message_id": message_id})


@dataclass
class Form:
    operation: str
    values: list = field(default_factory=list)
    end: float = field(default_factory=lambda: time.monotonic() + 600)


class InstallerBot:
    def __init__(self, api, state, http):
        self.api = api
        self.state = state
        self.http = http
        self.form = None
        self.work = None

    async def gateway(self, url, key, route, payload=None):
        try:
            response = await self.http.request(
                "POST" if payload is not None else "GET", url + "/gateway/" + route,
                headers={"Authorization": "Bearer " + key}, json=payload,
                timeout=22 if payload is not None else 5,
            )
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def panel(self, chat):
        selected = self.state.selected()
        if not selected:
            await self.api.send(chat, "Сначала установите Aster через /install.", KEYBOARD)
            return
        url, key = selected
        health = await self.gateway(url, key, "status")
        if not health or not health.get("ready"):
            await self.api.send(chat, f"Панель {url} пока недоступна. Проверьте TCP 80/443 и выдачу сертификата.", KEYBOARD)
            return
        keyboard = {"inline_keyboard": [[{"text": "Открыть Aster", "web_app": {"url": url + "/"}}]]}
        text = "Панель Aster готова." if health.get("model") else (
            "Панель запущена. В настройках укажите Base URL и ID модели. Для провайдера с авторизацией добавьте API-ключ."
        )
        await self.api.send(chat, text + "\n" + url, keyboard)

    async def relay(self, update, chat, command, text):
        selected = self.state.selected()
        if not selected:
            await self.api.send(chat, "Сначала установите и выберите VPS через /install или /use.", KEYBOARD)
            return
        if command == "/ask":
            _, _, prompt = text.partition(" ")
            mime = "text" if "text" in update["message"] else "caption"
            if not prompt.strip() and not any(key in update["message"] for key in ("document", "photo")):
                await self.api.send(chat, "Введите /ask и текст задания или приложите файл с подписью /ask.")
                return
            update = {**update, "message": {**update["message"], mime: prompt.strip()}}
        result = await self.gateway(*selected, "update", update)
        if not result or result.get("ok") is not True:
            await self.api.send(chat, "Не удалось подтвердить ответ Aster. Проверьте /jobs и доступность панели.")

    async def handle(self, update):
        message = update.get("message") or {}
        chat = message.get("chat", {})
        user = message.get("from", {}).get("id")
        if chat.get("type") != "private" or not user:
            return
        ident = chat["id"]
        raw = message.get("text") or message.get("caption") or ""
        text = raw.strip()
        first, _, rest = text.partition(" ")
        command = first.split("@", 1)[0].lower()
        if not self.state.owner():
            if command == "/claim" and rest:
                await self.api.erase(ident, message["message_id"])
                if self.state.claim(user, rest.strip()):
                    await self.api.send(ident, "Telegram-аккаунт привязан.\n\n" + HELP, KEYBOARD)
                else:
                    await self.api.send(ident, "Код неверен или истёк. Получите новый в консоли установщика.")
            elif command == "/start":
                await self.api.send(ident, "Нужен одноразовый код из консоли установщика. Отправьте /claim КОД.")
            return
        if user != self.state.owner():
            return
        if command == "/cancel":
            self.form = None
            await self.api.send(ident, "Форма ввода закрыта. Запущенная операция на VPS продолжит выполняться.", KEYBOARD)
            return
        if self.form and len(self.form.values) == 3:
            await self.accept_form(ident, message, raw)
            return
        if command in {"/install", "/load"}:
            if self.work and not self.work.done():
                await self.api.send(ident, "Дождитесь завершения текущей операции с VPS.")
                return
            self.form = Form(command[1:])
            await self.api.send(ident, PROMPTS[0])
            return
        if self.form:
            await self.accept_form(ident, message, raw)
            return
        if command in {"/start", "/help"}:
            await self.api.send(ident, HELP, KEYBOARD)
        elif command == "/panel":
            await self.panel(ident)
        elif command == "/servers":
            rows = self.state.servers()
            info = "\n".join(f"№{row['id']} — {row['url']}" for row in rows)
            await self.api.send(ident, info or "Установленных VPS пока нет.", KEYBOARD)
        elif command == "/use":
            if re.fullmatch(r"[0-9]{1,12}", rest) and self.state.choose(int(rest)):
                await self.panel(ident)
            else:
                await self.api.send(ident, "Укажите номер из /servers, например /use 1.")
        elif command in {"/ask", "/jobs", "/result", "/stop"}:
            await self.relay(update, ident, command, text)

    async def accept_form(self, chat, message, raw):
        form = self.form
        index = len(form.values)
        if time.monotonic() > form.end:
            self.form = None
            if index == 3 and raw:
                await self.api.erase(chat, message["message_id"])
            await self.api.send(chat, "Время ввода истекло. Начните снова через /install или /load.", KEYBOARD)
            return
        if not raw or "text" not in message:
            await self.api.send(chat, "Отправьте значение обычным текстом.")
            return
        if index == 0:
            try:
                form.values.append(ssh.Target.parse(raw))
            except ValueError:
                await self.api.send(chat, PROMPTS[0])
                return
        elif index == 1:
            login = raw.strip()
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}", login):
                await self.api.send(chat, PROMPTS[1])
                return
            form.values.append(login)
        elif index == 2:
            fingerprint = raw.strip()
            if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint):
                await self.api.send(chat, PROMPTS[2])
                return
            form.values.append(fingerprint)
        else:
            try:
                await self.api.erase(chat, message["message_id"])
            except TelegramUnavailable:
                self.form = None
                await self.api.send(chat, "Не удалось удалить сообщение с паролем. Удалите его в чате и начните операцию заново.")
                return
            self.form = None
            if len(raw) > 512 or "\n" in raw or "\r" in raw:
                await self.api.send(chat, "Пароль должен быть одной строкой до 512 символов. Начните операцию заново.")
                return
            self.work = asyncio.create_task(self.operation(chat, form, raw))
            return
        await self.api.send(chat, PROMPTS[len(form.values)])

    async def operation(self, chat, form, password):
        target, login, expected = form.values
        key = self.state.gateway_key(target.url) if form.operation == "install" else ""
        loop = asyncio.get_running_loop()
        progress = asyncio.Queue()

        def remote():
            with ssh.connection(target, login, password, expected) as transport:
                if form.operation == "load":
                    return ssh.metrics(transport)
                ssh.install(transport, login, target, self.api.token, self.state.owner(), key,
                            lambda message: loop.call_soon_threadsafe(progress.put_nowait, message))

        async def report():
            while True:
                message = await progress.get()
                with contextlib.suppress(TelegramUnavailable):
                    await self.api.send(chat, message)
                progress.task_done()

        worker = asyncio.create_task(report())
        try:
            progress.put_nowait("Проверяю SSH-соединение")
            result = await asyncio.to_thread(remote)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(progress.join(), 5)
            if form.operation == "load":
                await self.api.send(chat, f"Загрузка VPS {target.address}\n\n{result}", KEYBOARD)
                return
            ident = self.state.register(target.url, key)
            for attempt in range(8):
                result = await self.gateway(target.url, key, "status")
                if result and result.get("ready"):
                    break
                if attempt < 7:
                    await asyncio.sleep(4)
            if result and result.get("ready"):
                await self.api.send(chat, f"Aster развёрнут. VPS №{ident} выбран.")
                await self.panel(chat)
            else:
                await self.api.send(
                    chat, f"Контейнеры Aster запущены. VPS №{ident} добавлен. HTTPS ещё недоступен. "
                    f"Проверьте входящие TCP 80/443 и откройте /panel после выдачи сертификата.\n{target.url}", KEYBOARD,
                )
        except ssh.OperationError as error:
            with contextlib.suppress(TelegramUnavailable):
                await self.api.send(chat, str(error), KEYBOARD)
        except (TelegramUnavailable, OSError):
            print("Операция VPS прервана из-за проблем с подключением.", flush=True)
        except Exception as error:
            print("Операция VPS: " + type(error).__name__, flush=True)
            with contextlib.suppress(TelegramUnavailable):
                await self.api.send(chat, "Операция завершилась внутренней ошибкой. Проверьте журнал установщика.", KEYBOARD)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def run(self):
        offset = 0
        while True:
            try:
                updates = await self.api.call("getUpdates", {"timeout": 27, "offset": offset, "allowed_updates": ["message"]})
            except TelegramUnavailable as error:
                print(str(error), flush=True)
                await asyncio.sleep(5)
                continue
            for update in updates:
                try:
                    await self.handle(update)
                except TelegramUnavailable:
                    print("Не удалось отправить ответ Telegram.", flush=True)
                except Exception as error:
                    print("Обработка Telegram: " + type(error).__name__, flush=True)
                offset = max(offset, update["update_id"] + 1)
