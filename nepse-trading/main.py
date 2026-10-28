"""
main.py — nepse-trading
Starts the trading API (port 8767), register/credentials UI (port 8768),
Telegram bot, and Discord bot.
The bots are long-running; the APIs serve engine requests and web UI.
"""
import logging
import threading
import uvicorn
from trading_api import app
from register_api import app as register_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TRADING] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _start_api():
    uvicorn.run(app, host="0.0.0.0", port=8767, log_level="info")


def _start_register_api():
    uvicorn.run(register_app, host="0.0.0.0", port=8768, log_level="info")


def _start_telegram():
    from modules.telegram_bot import main as run_telegram
    run_telegram()


def _start_discord():
    from modules.discord_bot import main as run_discord
    run_discord()


if __name__ == "__main__":
    threads = [
        threading.Thread(target=_start_api,          name="trading-api",    daemon=False),
        threading.Thread(target=_start_register_api, name="register-api",   daemon=False),
        threading.Thread(target=_start_telegram,     name="telegram-bot",   daemon=False),
        threading.Thread(target=_start_discord,      name="discord-bot",    daemon=False),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
