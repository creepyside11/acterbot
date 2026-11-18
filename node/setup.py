#!/usr/bin/env python3
"""Generate the VPS configuration or look up the owner ID using a fresh bot."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}")
DIRECTORY = Path(__file__).resolve().parent


def parse_address(raw):
    value = raw.strip().removeprefix("https://").rstrip("/")
    if not value:
        return "", ""
    value = value.removeprefix("[").removesuffix("]").lower()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        domain = value.encode("idna").decode()
        if (
            len(domain) > 253 or "." not in domain or
            not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in domain.split("."))
        ):
            raise ValueError("Введите публичный IP или домен без порта и пути")
        return domain, domain
    if not ip.is_global or ip.is_multicast or "%" in value or (ip.version == 6 and ip.ipv4_mapped):
        raise ValueError("Для публичного HTTPS нужен внешний адрес VPS")
    return (f"[{ip}]" if ip.version == 6 else str(ip)), str(ip)


def ask_token(optional=False):
    prompt = "Токен бота (Enter — пропустить): " if optional else "Токен бота: "
    value = getpass.getpass(prompt).strip()
    if not value and optional:
        return value
    if not TOKEN.fullmatch(value):
        raise ValueError("Токен должен быть выдан BotFather")
    return value


def telegram_id():
    token = ask_token()
    print("Отправьте /start своему боту. Ожидание до 25 секунд…")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=b'{"timeout": 25, "allowed_updates": ["message"]}',
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            updates = json.load(response)
    except urllib.error.HTTPError as err:
        raise ValueError(f"Telegram ответил HTTP {err.code}. Проверьте токен, webhook и другие запущенные экземпляры бота") from None
    except (OSError, ValueError):
        raise ValueError("Сервер Telegram недоступен") from None
    people = {}
    for update in updates.get("result", []):
        message = update.get("message", {})
        user = message.get("from", {})
        if message.get("chat", {}).get("type") == "private" and user.get("id"):
            people[user["id"]] = " ".join(filter(None, (user.get("first_name"), user.get("last_name"), user.get("username"))))
    if not people:
        print("Личных сообщений пока нет. Отправьте /start и повторите команду.")
    for ident, name in people.items():
        print(f"{ident}   {name}")


def configure():
    target = DIRECTORY / ".env"
    if target.exists():
        raise ValueError(".env уже существует. Для смены параметров отредактируйте его")
    address, host = parse_address(input("Публичный IP или домен VPS (Enter — локальный режим): "))
    token = ask_token(optional=True)
    owner = ""
    if token:
        owner = input("Ваш числовой Telegram ID: ").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
            raise ValueError("Укажите положительный числовой Telegram ID")
    password = secrets.token_urlsafe(26)
    variables = {
        "PANEL_PASSWORD": password, "DATA_SECRET": secrets.token_hex(32),
        "BROWSER_TOKEN": secrets.token_hex(32), "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_OWNER_ID": owner, "PUBLIC_URL": "https://" + address if address else "",
        "PUBLIC_HOST": address, "TLS_HOST": host,
    }
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as env:
        env.writelines(f"{key}={value}\n" for key, value in variables.items())
    print(f"\nПароль панели: {password}")
    if address:
        print(f"Панель: https://{address}/")
        print("Команда запуска: docker compose --profile https up -d --build")
    else:
        print("Панель: http://127.0.0.1:8000/")
        print("Команда запуска: docker compose up -d --build")


def main():
    parser = argparse.ArgumentParser(description="Настройка Aster")
    parser.add_argument("command", nargs="?", default="configure", choices=["configure", "telegram-id"])
    args = parser.parse_args()
    try:
        configure() if args.command == "configure" else telegram_id()
    except (ValueError, OSError, EOFError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
