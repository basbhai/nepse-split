"""
AI/claude_router.py
===================
Per-call transport router for `claude_analyst`'s Claude call.

`AI/__init__.py` exports THIS module's `ask_claude`, so every
`from AI import ask_claude` (only `claude_analyst.py` does) goes through here.
Direct importers of `AI.openrouter.ask_claude` (e.g. `agents/wait_agent`) are
unaffected.

Backend is chosen per call from the `CLAUDE_ANALYST_BACKEND` setting
(dashboard-toggleable, no restart):

    openrouter  (default)  existing OpenRouter chain, unchanged
    cli                    `claude -p` subscription transport only
    auto                   CLI first; on None, fall back to the OpenRouter chain

Fail-safe: if the selected path returns None the contract is preserved (caller
logs + `continue`s, the flag row stays PENDING_CLAUDE and is retried) — it never
fails open. `auto` adds a resilience layer, not a new failure mode.

Blast-radius guard: any call whose `context` is not `claude_analyst` is routed
straight to OpenRouter regardless of the setting — this router only ever changes
the analyst path.
"""

import logging
import time
from typing import Optional

from AI.openrouter import ask_claude as _ask_claude_openrouter
from AI.claude_cli import ask_claude_cli as _ask_claude_cli

log = logging.getLogger(__name__)

_ANALYST_CONTEXT = "claude_analyst"
_SETTING_KEY     = "CLAUDE_ANALYST_BACKEND"
_DEFAULT_BACKEND = "openrouter"
_VALID_BACKENDS  = ("openrouter", "cli", "auto")

# --- lightweight circuit breaker for `auto` -------------------------------
# After N consecutive CLI failures, `auto` skips the CLI entirely for a short
# cooldown so a broken CLI can't add ~45s latency to every analyst call.
_CB_THRESHOLD      = 3
_CB_COOLDOWN_SEC   = 1800          # 30 min
_cli_fail_streak   = 0
_cli_cooldown_until = 0.0


def get_analyst_backend() -> str:
    """Public: the currently configured CLAUDE_ANALYST_BACKEND ('openrouter'/'cli'/'auto').

    Exposed (not just used internally by ask_claude below) so other parts of
    the analyst pipeline — e.g. claude_analyst._load_lessons()'s decision to
    skip prompt-size limits on the CLI path — can read the same setting
    through one validated helper instead of re-implementing the fallback logic.
    """
    try:
        from sheets import get_setting
        val = (get_setting(_SETTING_KEY, _DEFAULT_BACKEND) or "").strip().lower()
    except Exception as exc:
        log.warning("[claude_router] could not read %s (%s) — using %s",
                    _SETTING_KEY, exc, _DEFAULT_BACKEND)
        return _DEFAULT_BACKEND
    if val not in _VALID_BACKENDS:
        log.warning("[claude_router] %s=%r not recognised — using %s",
                    _SETTING_KEY, val, _DEFAULT_BACKEND)
        return _DEFAULT_BACKEND
    return val


def _cli_breaker_open() -> bool:
    return time.monotonic() < _cli_cooldown_until


def _record_cli_result(ok: bool) -> None:
    global _cli_fail_streak, _cli_cooldown_until
    if ok:
        _cli_fail_streak = 0
        _cli_cooldown_until = 0.0
        return
    _cli_fail_streak += 1
    if _cli_fail_streak >= _CB_THRESHOLD:
        _cli_cooldown_until = time.monotonic() + _CB_COOLDOWN_SEC
        log.warning("[claude_router] CLI failed %d× in a row — `auto` will skip "
                    "CLI for %d min", _cli_fail_streak, _CB_COOLDOWN_SEC // 60)


def ask_claude(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 1200,
    temperature: float = 0.2,
    context: str = "claude",
    symbol: str = "",
) -> Optional[dict]:
    """Route the analyst's Claude call to the configured backend. Never raises."""
    if context != _ANALYST_CONTEXT:
        # Not the analyst — this router has no business here.
        return _ask_claude_openrouter(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, context=context, symbol=symbol,
        )

    backend = get_analyst_backend()

    if backend == "openrouter":
        return _ask_claude_openrouter(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, context=context, symbol=symbol,
        )

    if backend == "cli":
        log.info("[claude_router] backend=cli (%s)", symbol or "-")
        return _ask_claude_cli(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, context=context, symbol=symbol,
        )

    # backend == "auto"
    if _cli_breaker_open():
        log.info("[claude_router] backend=auto but CLI breaker open — OpenRouter (%s)",
                 symbol or "-")
        return _ask_claude_openrouter(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, context=context, symbol=symbol,
        )

    log.info("[claude_router] backend=auto — trying CLI first (%s)", symbol or "-")
    result = _ask_claude_cli(
        prompt, system=system, max_tokens=max_tokens,
        temperature=temperature, context=context, symbol=symbol,
    )
    _record_cli_result(result is not None)
    if result is not None:
        log.info("[claude_router] auto: CLI served (%s)", symbol or "-")
        return result

    log.warning("[claude_router] auto: CLI returned None — falling back to OpenRouter (%s)",
                symbol or "-")
    return _ask_claude_openrouter(
        prompt, system=system, max_tokens=max_tokens,
        temperature=temperature, context=context, symbol=symbol,
    )
