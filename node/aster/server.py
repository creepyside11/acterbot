import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, ProviderError, completion, result_text
from .config import MAX_BODY, MAX_FILE, MAX_FILES, Config, ModelConfig
from .store import Store
from .telegram import Telegram

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    config = Config()
    store = Store(config)
    store.initialize()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        agent = Agent(store, config.browser_url, config.browser_token, client)
        app.state.store = store
        app.state.config = config
        app.state.agent = agent
        app.state.client = client
        tasks = [asyncio.create_task(agent.worker())]
        if config.telegram_token:
            telegram = Telegram(config, store, agent, client)
            app.state.telegram = telegram
            tasks.append(asyncio.create_task(telegram.notify()))
            if config.telegram_poll:
                tasks.append(asyncio.create_task(telegram.poll()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def headers_and_limits(request, call_next):
    length = request.headers.get("content-length")
    if request.method in {"POST", "PUT"} and (
        request.headers.get("transfer-encoding") or (length and (not length.isdecimal() or len(length) > 10 or int(length) > MAX_BODY))
    ):
        return JSONResponse({"detail": "Размер запроса превышает лимит"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    ))
    return response


@app.exception_handler(RequestValidationError)
async def invalid_input(request, error):
    first = error.errors()[0]
    field = str(first["loc"][-1])
    detail = first["msg"] if first["type"] == "value_error" else "Проверьте значение в поле «" + field + "»"
    return JSONResponse({"detail": detail.removeprefix("Value error, ")}, status_code=422)


def authorized(request: Request, authorization: str = Header(default="")):
    mode, _, token = authorization.partition(" ")
    if mode.lower() != "bearer" or not request.app.state.config.valid_session(token):
        raise HTTPException(401, "Войдите в панель")


def public_settings(config, model):
    values = model.model_dump(exclude={"api_key"})
    return {
        **values, "has_key": bool(model.api_key), "browser": bool(config.browser_url),
        "telegram": bool(config.telegram_token), "public_url": config.public_url,
        "workspace": config.workspace is not None,
    }


class Login(BaseModel):
    password: str = Field(default="", max_length=1000)
    init_data: str = Field(default="", max_length=12_000)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/login")
async def login(payload: Login):
    config = app.state.config
    ok = config.valid_telegram(payload.init_data) if payload.init_data else hmac.compare_digest(
        config.password.encode(), payload.password.encode()
    )
    if not ok:
        await asyncio.sleep(0.35)
        raise HTTPException(401, "Неверный пароль или ваш Telegram-аккаунт не входит в список доступа")
    return {"token": config.session()}


@app.get("/api/settings", dependencies=[Depends(authorized)])
async def get_settings():
    return public_settings(app.state.config, app.state.store.model())


@app.put("/api/settings", dependencies=[Depends(authorized)])
async def save_settings(value: ModelConfig):
    return public_settings(app.state.config, app.state.store.set_model(value))


@app.post("/api/settings/test", dependencies=[Depends(authorized)])
async def check_settings():
    model = app.state.store.model()
    if not model.model:
        raise HTTPException(422, "Сначала сохраните ID модели")
    try:
        async with asyncio.timeout(45):
            message = await completion(app.state.client, model, [{"role": "user", "content": "Ответь одним словом: OK"}])
    except ProviderError as error:
        raise HTTPException(502, str(error)) from error
    except TimeoutError as error:
        raise HTTPException(504, "Тест не завершился за 45 секунд") from error
    return {"reply": result_text(message)[:160]}


@app.get("/api/jobs", dependencies=[Depends(authorized)])
async def jobs(before: int | None = Query(None, gt=0)):
    return app.state.store.list_jobs(before=before)


def find_job(job):
    item = app.state.store.job(job)
    if not item:
        raise HTTPException(404, "Задача не найдена")
    return item


@app.get("/api/jobs/{job}", dependencies=[Depends(authorized)])
async def job_detail(job: int):
    item = find_job(job)
    return {key: value for key, value in item.items() if key not in {"source", "chat", "notified"}}


@app.post("/api/jobs", status_code=201, dependencies=[Depends(authorized)])
async def new_job(
    prompt: str = Form(max_length=16_000),
    parent: int | None = Form(None),
    files: list[UploadFile] | None = File(None),
):
    if not app.state.store.model().model:
        raise HTTPException(422, "Укажите ID модели в настройках")
    if len(files or []) > MAX_FILES:
        raise HTTPException(413, "Можно прикрепить до четырёх файлов")
    uploads = []
    for file in files or []:
        data = await file.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise HTTPException(413, "Каждый файл должен быть не больше 8 МБ")
        uploads.append((file.filename or "file.txt", data))
    try:
        job = app.state.store.create_job(prompt, uploads, parent=parent)
    except LookupError as error:
        raise HTTPException(404, "Исходная задача не найдена") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    app.state.agent.wake.set()
    return {"id": job}


@app.post("/api/jobs/{job}/cancel", dependencies=[Depends(authorized)])
async def stop_job(job: int):
    find_job(job)
    if not app.state.agent.cancel(job):
        raise HTTPException(409, "Задача уже завершилась")
    return {"ok": True}


@app.post("/api/jobs/{job}/retry", status_code=201, dependencies=[Depends(authorized)])
async def retry_job(job: int):
    item = find_job(job)
    if item["status"] in {"queued", "running"}:
        raise HTTPException(409, "Задача ещё активна")
    if not app.state.store.model().model:
        raise HTTPException(422, "Укажите ID модели в настройках")
    try:
        new_id = app.state.store.create_job(item["prompt"], parent=item["parent"], retry=job)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    app.state.agent.wake.set()
    return {"id": new_id}


def signed_download(ident, expiry):
    payload = f"download:{ident}:{expiry}"
    return hmac.digest(app.state.config.signing, payload.encode(), "sha256").hex()


@app.post("/api/files/{ident}/link", dependencies=[Depends(authorized)])
async def file_link(ident: str):
    if not app.state.store.file(ident):
        raise HTTPException(404, "Файл не найден")
    expiry = int(time.time()) + 90
    return {"url": f"/download/{ident}?e={expiry}&s={signed_download(ident, expiry)}"}


@app.get("/download/{ident}")
async def download(ident: str, e: int, s: str):
    if e < time.time() or not hmac.compare_digest(s.encode(), signed_download(ident, e).encode()):
        raise HTTPException(401, "Срок ссылки истёк")
    item = app.state.store.file(ident)
    if item is None:
        raise HTTPException(404, "Файл не найден")
    return Response(item["data"], media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''" + quote(item["name"]),
        "Access-Control-Allow-Origin": "https://web.telegram.org",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })



def gateway_auth(authorization: str = Header(default="")):
    key = app.state.config.gateway_key
    if not key or not hmac.compare_digest(authorization.encode(), ("Bearer " + key).encode()):
        raise HTTPException(401, "Доступ шлюза запрещён")


@app.get("/gateway/status", dependencies=[Depends(gateway_auth)])
async def gateway_status():
    return {"ready": True, "model": bool(app.state.store.model().model)}


@app.post("/gateway/update", dependencies=[Depends(gateway_auth)])
async def gateway_update(request: Request):
    size = request.headers.get("content-length")
    if not size or not size.isdecimal() or len(size) > 6 or int(size) > 262_144:
        raise HTTPException(413, "Слишком большое сообщение")
    try:
        payload = await request.json()
        if not isinstance(payload["message"], dict) or type(payload["update_id"]) is not int:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Некорректное обновление Telegram") from None
    await app.state.telegram.handle(payload)
    return {"ok": True}
