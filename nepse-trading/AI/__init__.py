"""
AI stub for nepse-trading.
ask_free: regex-based trade command parser — no external AI call needed.
Wire in real AI if natural language parsing is required later.
"""
import re


def ask_free(prompt: str, system: str = "", context: str = "") -> str:
    """
    Parse '/buy SYMBOL SHARES PRICE' or '/sell SYMBOL SHARES PRICE' via regex.
    Returns JSON string matching the format telegram_bot.parse_trade_command expects.
    # ponytail: regex only — add AI call if free-text trade commands are needed
    """
    text  = prompt.strip().lstrip("/")
    parts = text.split()
    if len(parts) < 4:
        return '{"error": "Cannot parse: expected SYMBOL SHARES PRICE"}'

    action = parts[0].lower()
    if action not in ("buy", "sell"):
        return '{"error": "Cannot parse: unknown action"}'

    symbol = parts[1].upper()

    try:
        shares = float(parts[2])
        price  = float(parts[3])
    except ValueError:
        return '{"error": "Cannot parse: shares and price must be numbers"}'

    import json
    return json.dumps({"action": action, "symbol": symbol, "shares": shares, "price": price})
