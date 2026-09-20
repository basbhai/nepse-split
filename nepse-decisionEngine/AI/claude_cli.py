"""
AI/claude_cli.py
================
Second transport for reaching Claude from `claude_analyst`: shell out to the
`claude` CLI in headless print mode (`claude -p --output-format json`) instead
of the OpenRouter HTTP call in `AI/openrouter.py`.

Why: the CLI authenticates from `~/.claude/.credentials.json` (the Claude
subscription), independent of the OpenRouter API key / billing. Same pattern
already in production for `agents/claude_wait_monitor/`.

Contract — identical to `AI.openrouter.ask_claude`:
    ask_claude_cli(prompt, system=..., context="claude_analyst", symbol="NABIL")
      -> parsed dict   on success
      -> None          on ANY failure (non-zero exit, timeout, is_error,
                       empty/!JSON result). The caller's existing `None`
                       handling (log + `continue`, row stays PENDING_CLAUDE,
                       retried next cycle) is the fail-safe — it never fails open.

Sealed call: zero tools, no MCP servers, single-turn (structural — no agentic
loop is possible when no tools exist), cwd `/tmp` (no repo CLAUDE.md / project
settings), env stripped of Anthropic API keys so the call can only resolve to
the subscription. `max_tokens` / `temperature` are accepted for signature
parity with the OpenRouter path but the CLI exposes no such knobs — see
SESSION-SUMMARY.md for the study showing this is not load-bearing for this task.
"""

import json
import logging
import os
import re
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLAUDE_CLI_BIN   = os.getenv("CLAUDE_CLI_BIN", "/home/shanvi/.local/bin/claude")
CLAUDE_CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL", "claude-sonnet-5")
# The real 24 KB analyst prompt on Sonnet 5 measured ~45s end-to-end at
# merge time (Phase 3 smoke), vs the ~20s OpenRouter baseline — 75s was
# meant as headroom over that. It stopped being headroom once learning_hub
# lesson volume grew (see claude_analyst._load_lessons / CLI_LESSON_LIMIT):
# on 2026-09-15, with the lesson list capped at 25, real calls landed at
# 71-75s — i.e. right on the timeout edge, with ~50% of first attempts
# timing out and falling through to a second 75s-capped try. 120s restores
# genuine margin at that lesson volume. See nepse-project/TODO.md Phase 3
# for the cycle-timeout math (systemd TimeoutStartSec raised to 600s
# alongside the original 75s value — re-check that budget if this changes
# again, since main.py runs on a 30-min timer with several candidates
# analyzed sequentially per cycle).
CLI_TIMEOUT_SEC  = int(os.getenv("CLAUDE_CLI_TIMEOUT_SEC", "120"))
CLI_MAX_ATTEMPTS = int(os.getenv("CLAUDE_CLI_MAX_ATTEMPTS", "2"))   # 1 try + 1 retry
CLI_CWD          = "/tmp"

_DEFAULT_SYSTEM = (
    "You are a senior NEPSE quantitative analyst. "
    "You respond ONLY in valid JSON. No markdown fences. "
    "You never recommend a trade without a clear stop loss."
)

# Anthropic auth vars stripped from the child env so the call can ONLY resolve
# to the subscription credentials in ~/.claude/.credentials.json — never an
# accidental metered API key.
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    return env


def _strip_fences(raw: str) -> str:
    """Strip markdown code fences models sometimes add. Mirrors AI.openrouter."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1] if len(parts) > 1 else parts[0]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _extract_json(text: str) -> Optional[dict]:
    """result-text -> dict, tolerating fences and leading/trailing prose."""
    if not text:
        return None
    candidate = _strip_fences(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", candidate, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _run_cli(prompt: str, system_prompt: str, context: str) -> tuple[Optional[dict], str]:
    """
    One invocation of the CLI (no retry). Returns (envelope_dict, error_msg).
    envelope_dict is None on process-level failure.
    """
    argv = [
        CLAUDE_CLI_BIN,
        "-p",
        "--output-format", "json",
        "--model", CLAUDE_CLI_MODEL,
        "--system-prompt", system_prompt,
        "--exclude-dynamic-system-prompt-sections",
        "--allowedTools", "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
    ]
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=CLI_CWD,
            env=_child_env(),
            timeout=CLI_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return None, f"CLI timeout after {CLI_TIMEOUT_SEC}s"
    except FileNotFoundError:
        return None, f"CLI binary not found: {CLAUDE_CLI_BIN}"
    except Exception as exc:                              # pragma: no cover - defensive
        return None, f"CLI spawn error: {exc}"

    if proc.returncode != 0:
        return None, (
            f"CLI exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
        )

    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        return None, "CLI produced empty stdout"

    try:
        envelope = json.loads(raw_out)
    except json.JSONDecodeError as exc:
        # The envelope itself should always be valid JSON; tolerate a preamble.
        m = re.search(r"\{.*\}", raw_out, re.S)
        if not m:
            return None, f"CLI envelope not JSON: {exc}"
        try:
            envelope = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, f"CLI envelope not JSON: {exc}"

    if not isinstance(envelope, dict):
        return None, "CLI envelope not an object"

    return envelope, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def ask_claude_cli(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 1200,      # accepted for parity; CLI has no knob
    temperature: float = 0.2,    # accepted for parity; CLI has no knob
    context: str = "claude",
    symbol: str = "",
    log_call: bool = True,
) -> Optional[dict]:
    """
    Call Claude via the `claude` CLI. Returns parsed JSON dict or None.

    Same return contract as AI.openrouter.ask_claude. Never raises.
    """
    system_prompt = system or _DEFAULT_SYSTEM
    model_tag     = f"cli/{CLAUDE_CLI_MODEL}"

    t0          = time.monotonic()
    envelope    = None
    last_error  = ""
    for attempt in range(1, CLI_MAX_ATTEMPTS + 1):
        log.info("[%s] claude CLI attempt %d/%d (model=%s)",
                 context, attempt, CLI_MAX_ATTEMPTS, CLAUDE_CLI_MODEL)
        envelope, last_error = _run_cli(prompt, system_prompt, context)
        if envelope is not None:
            break
        log.warning("[%s] claude CLI attempt %d failed: %s",
                    context, attempt, last_error)
    latency_ms = (time.monotonic() - t0) * 1000

    def _log(response_raw: Optional[str], success: bool, error_msg: str,
             cost_usd: str = "", tokens_in: str = "", tokens_out: str = "") -> None:
        if not log_call:
            return
        try:
            from AI.openrouter import _log_ai_call
            _log_ai_call(
                model_tag, context, system_prompt, prompt, response_raw,
                success, error_msg, latency_ms, symbol,
                backend="cli", cost_usd=cost_usd,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )
        except Exception as exc:                          # pragma: no cover
            log.warning("[%s] claude CLI _log_ai_call failed (non-fatal): %s",
                        context, exc)

    if envelope is None:
        _log(None, False, last_error or "CLI failed")
        return None

    usage      = envelope.get("usage") or {}
    cost_usd   = str(envelope.get("total_cost_usd", "") or "")
    tokens_in  = str(usage.get("input_tokens", "") or "")
    tokens_out = str(usage.get("output_tokens", "") or "")

    if envelope.get("is_error"):
        _log(str(envelope.get("result", ""))[:2000], False,
             f"CLI is_error: {str(envelope.get('result',''))[:200]}",
             cost_usd, tokens_in, tokens_out)
        return None

    result_text = envelope.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        _log(None, False, "CLI envelope has empty result",
             cost_usd, tokens_in, tokens_out)
        return None

    parsed = _extract_json(result_text)
    if parsed is None:
        log.error("[%s] claude CLI returned non-JSON result: %s",
                  context, result_text[:300])
        _log(result_text[:4000], False, "result not valid JSON",
             cost_usd, tokens_in, tokens_out)
        return None

    _log(result_text[:8000], True, "", cost_usd, tokens_in, tokens_out)
    log.info("[%s] claude CLI ok (%.0f ms, in=%s out=%s ~$%s)",
             context, latency_ms, tokens_in, tokens_out, cost_usd)
    return parsed
