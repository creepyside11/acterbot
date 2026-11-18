"""Chromium service. Its HTTP API is reachable only by the application network."""

import asyncio
import base64
import hmac
import ipaddress
import os
import re
import socket
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote_plus, urlsplit

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import Error, async_playwright
from pydantic import BaseModel, Field

TOKEN = os.environ.get("BROWSER_TOKEN", "")
PUBLIC_HOST = urlsplit(os.environ.get("PUBLIC_URL", "")).hostname
CHROMIUM = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium-headless-shell")
PROXY_PORT = 9851
NAVIGATION_TIMEOUT = 20_000


def public_ip(value):
    address = ipaddress.ip_address(value)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and (
        address.version == 4 or address in ipaddress.ip_network("2000::/3")
    )


async def resolve(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(record[4][0] for record in records))


def approved_url(value):
    if len(value) > 2048 or any(c.isspace() or c in "\\\x00" for c in value):
        raise ValueError("Недопустимый адрес страницы")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.port not in (None, 443):
        raise ValueError("Откройте публичный адрес HTTPS")
    return value


class PublicProxy:
    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.connections = asyncio.Semaphore(32)

    async def tunnel(self, reader, writer):
        remote = None
        established = False
        try:
            async with self.connections, asyncio.timeout(120):
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                method, authority, version = header.split(b"\r\n", 1)[0].decode("ascii").split()
                if method != "CONNECT" or version not in ("HTTP/1.0", "HTTP/1.1"):
                    raise ValueError
                url = urlsplit("https://" + authority)
                host = (url.hostname or "").rstrip(".").lower()
                if not host or url.port != 443 or url.username or url.password or url.path or url.query or url.fragment:
                    raise ValueError
                if not re.fullmatch(r"[a-z0-9.:-]{1,253}", host) or host == PUBLIC_HOST:
                    raise ValueError
                if PUBLIC_HOST and not self.blocked:
                    self.blocked.update(await asyncio.wait_for(resolve(PUBLIC_HOST), 5))
                addresses = await asyncio.wait_for(resolve(host), 5)
                if not addresses or any(addr in self.blocked or not public_ip(addr) for addr in addresses):
                    raise ValueError
                for address in addresses[:4]:
                    try:
                        source, remote = await asyncio.wait_for(asyncio.open_connection(address, 443), 3)
                        break
                    except (OSError, TimeoutError):
                        continue
                if remote is None:
                    raise ValueError
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                established = True

                async def forward(origin, destination):
                    while data := await origin.read(65536):
                        destination.write(data)
                        await destination.drain()

                directions = [asyncio.create_task(forward(reader, remote)), asyncio.create_task(forward(source, writer))]
                try:
                    await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for direction in directions:
                        direction.cancel()
                    await asyncio.gather(*directions, return_exceptions=True)
        except (ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            if not established and not writer.is_closing():
                with suppress(OSError):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
        finally:
            if remote:
                remote.close()
            writer.close()


class BrowserSession:
    async def start(self, chromium):
        self.lock = asyncio.Lock()
        self.context = await chromium.new_context(
            viewport={"width": 1280, "height": 800}, service_workers="block", accept_downloads=False,
        )
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.page = await self.context.new_page()
        self.page.set_default_timeout(8000)
        self.links = []
        self.touched = time.monotonic()
        return self

    @staticmethod
    async def route(route):
        if route.request.method in ("GET", "HEAD"):
            try:
                approved_url(route.request.url)
            except ValueError:
                pass
            else:
                await route.continue_()
                return
        await route.abort()

    async def open(self, url):
        url = approved_url(url)
        self.links = []
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        approved_url(self.page.url)
        info = await self.read()
        if response:
            info["http_status"] = response.status
        return info

    async def read(self):
        data = await self.page.evaluate("""() => {
          const body = document.body;
          const anchors = Array.from(document.links).slice(0, 250);
          return {
            text: body ? body.innerText.slice(0, 13000) : '',
            links: anchors.map(a => [a.innerText.trim().slice(0, 180), a.href.slice(0, 2048)])
          };
        }""")
        links = []
        seen = set()
        for title, href in data["links"]:
            if not title or href in seen:
                continue
            try:
                approved_url(href)
            except ValueError:
                continue
            seen.add(href)
            links.append({"index": len(links) + 1, "label": title, "url": href})
            if len(links) == 35:
                break
        self.links = links
        return {"title": (await self.page.title())[:220], "url": self.page.url, "text": data["text"], "links": links}

    async def follow(self, index):
        if index < 1 or index > len(self.links):
            raise ValueError("Выберите номер ссылки из последнего списка")
        return await self.open(self.links[index - 1]["url"])


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 32:
        raise RuntimeError("Настройте BROWSER_TOKEN")
    blocked = ()
    if PUBLIC_HOST:
        with suppress(OSError, TimeoutError):
            blocked = await asyncio.wait_for(resolve(PUBLIC_HOST), 5)
    proxy = PublicProxy(blocked)
    async with await asyncio.start_server(proxy.tunnel, "127.0.0.1", PROXY_PORT, limit=16_384):
        async with async_playwright() as playwright:
            chromium = await playwright.chromium.launch(
                executable_path=CHROMIUM, headless=True,
                proxy={"server": f"http://127.0.0.1:{PROXY_PORT}", "bypass": "<-loopback>"},
                args=["--disable-quic", "--disable-dev-shm-usage", "--disable-background-networking",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost"],
            )
            app.state.chromium = chromium
            app.state.sessions = {}
            app.state.lock = asyncio.Lock()
            try:
                yield
            finally:
                await chromium.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Action(BaseModel):
    session: str = Field(pattern=r"^[0-9a-f]{32}$")
    action: str = Field(max_length=35)
    value: str = Field(default="", max_length=2048)
    index: int = Field(default=0, ge=-10_000, le=10_000)


@app.get("/health")
async def health():
    if not app.state.chromium.is_connected():
        raise HTTPException(503, "Chromium offline")
    return {"ok": True}


@app.post("/action")
async def action(command: Action, authorization: str = Header(default="")):
    if not hmac.compare_digest(authorization.encode(), ("Bearer " + TOKEN).encode()):
        raise HTTPException(401, "Unauthorized")
    if command.action not in {"close", "open", "search", "read", "follow", "scroll", "screenshot"}:
        raise HTTPException(400, "Неизвестное действие браузера")
    try:
        async with asyncio.timeout(26):
            async with app.state.lock:
                sessions = app.state.sessions
                expired = [
                    key for key, item in sessions.items()
                    if time.monotonic() - item.touched > 600 and not item.lock.locked()
                ]
                for key in expired:
                    await sessions.pop(key).context.close()
                session = sessions.get(command.session)
                if command.action == "close":
                    if session:
                        sessions.pop(command.session)
                        await session.context.close()
                    return {"closed": True}
                if session is None:
                    if len(sessions) >= 4:
                        raise HTTPException(503, "Браузер занят")
                    session = await BrowserSession().start(app.state.chromium)
                    sessions[command.session] = session
                session.touched = time.monotonic()
            async with session.lock:
                if command.action == "open":
                    return await session.open(command.value)
                if command.action == "search":
                    return await session.open("https://duckduckgo.com/?q=" + quote_plus(command.value[:300]))
                if command.action == "follow":
                    return await session.follow(command.index)
                approved_url(session.page.url)
                if command.action == "scroll":
                    await session.page.mouse.wheel(0, command.index)
                    await session.page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                if command.action == "screenshot":
                    picture = await session.page.screenshot(type="png", timeout=8000, animations="disabled")
                    return {"image": base64.b64encode(picture).decode(), "url": session.page.url}
                return await session.read()
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except (Error, TimeoutError) as error:
        raise HTTPException(502, "Страница недоступна, заблокирована или слишком долго отвечает") from error


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("BROWSER_HOST", "127.0.0.1"), port=8010, access_log=False)
