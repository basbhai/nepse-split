"""
notifier.py — nepse-trading
Sync send helpers for Telegram and Discord. Used by briefing.py and any
trading-service module that needs to push messages directly.
Not async — briefing runs as a script/thread, not a FastAPI handler.
"""
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
MAX_TG_LENGTH   = 4000
MAX_DISCORD_LENGTH = 1900


def send_tg(chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
    """Send message to a Telegram chat_id. Never raises."""
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    if len(text) > MAX_TG_LENGTH:
        text = text[:MAX_TG_LENGTH - 20] + "\n...(truncated)"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(TELEGRAM_API, json=payload, timeout=8)
        if resp.status_code == 200:
            return True
        if resp.status_code == 400 and "parse" in resp.text.lower():
            payload.pop("parse_mode", None)
            resp2 = requests.post(TELEGRAM_API, json=payload, timeout=8)
            return resp2.status_code == 200
        log.error("TG send %s: HTTP %d — %s", chat_id, resp.status_code, resp.text[:150])
        return False
    except Exception as e:
        log.error("TG send %s: %s", chat_id, e)
        return False


def send_discord_dm(discord_id: str, text: str) -> bool:
    """Send a private DM to a Discord user via bot REST API. Never raises."""
    if not DISCORD_BOT_TOKEN or not discord_id:
        return False
    discord_text = re.sub(r'(?<!\*)\*(?!\*)([^*]+)\*(?!\*)', r'**\1**', text)
    if len(discord_text) > MAX_DISCORD_LENGTH:
        discord_text = discord_text[:MAX_DISCORD_LENGTH - 20] + "\n...(truncated)"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers, json={"recipient_id": str(discord_id)}, timeout=10,
        )
        if r.status_code != 200:
            log.error("Discord DM channel failed %d for %s", r.status_code, discord_id)
            return False
        channel_id = r.json().get("id")
        r2 = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers, json={"content": discord_text}, timeout=10,
        )
        return r2.status_code in (200, 201)
    except Exception as e:
        log.error("Discord DM %s: %s", discord_id, e)
        return False
