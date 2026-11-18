import argparse
import asyncio
import os
import re

import httpx
from dotenv import load_dotenv

from .bot import InstallerBot, TelegramAPI
from .state import State


async def serve(token, state):
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        await InstallerBot(TelegramAPI(token, client), state, client).run()


def main():
    parser = argparse.ArgumentParser(description="Установщик Aster для VPS")
    parser.add_argument("command", choices=["run", "claim"], default="run", nargs="?")
    args = parser.parse_args()
    load_dotenv()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}", token):
        parser.error("Укажите Telegram-токен в BOT_TOKEN.")
    state = State(token)
    if not state.owner():
        code = state.issue_claim()
        if code:
            print(f"В личном Telegram-чате бота отправьте /claim {code} (действует 30 минут).", flush=True)
    elif args.command == "claim":
        print("Telegram-владелец уже привязан.", flush=True)
    if args.command == "run":
        try:
            asyncio.run(serve(token, state))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
