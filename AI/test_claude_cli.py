#!/usr/bin/env python3
"""
AI/test_claude_cli.py
=====================
Unit tests for the `claude -p` analyst backend (AI/claude_cli.py) and the
per-call transport router (AI/claude_router.py). No network, no real CLI, no DB
— `subprocess.run`, `_log_ai_call` and `get_setting` are all mocked.

Run:
    python -m unittest AI.test_claude_cli -v
    # or
    python AI/test_claude_cli.py
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AI import claude_cli
from AI import claude_router


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _envelope(result='{"action": "WAIT", "confidence": 40}', is_error=False,
              cost=0.041, tin=12000, tout=180):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result,
        "num_turns": 1,
        "total_cost_usd": cost,
        "usage": {"input_tokens": tin, "output_tokens": tout},
        "permission_denials": [],
    })


class ClaudeCliTests(unittest.TestCase):

    def setUp(self):
        # Silence the audit-log hop in every test unless a test opts in.
        p = mock.patch("AI.openrouter._log_ai_call")
        self.mock_log = p.start()
        self.addCleanup(p.stop)

    # ---- happy path ----------------------------------------------------
    def test_happy_path_returns_parsed_dict(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope())) as sr:
            out = claude_cli.ask_claude_cli("prompt", context="claude_analyst", symbol="NABIL")
        self.assertEqual(out, {"action": "WAIT", "confidence": 40})
        # sealed-call invariants
        argv = sr.call_args.args[0]
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "")
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv)
        self.assertEqual(sr.call_args.kwargs["cwd"], "/tmp")
        self.assertEqual(sr.call_args.kwargs["input"], "prompt")
        self.assertNotIn("ANTHROPIC_API_KEY", sr.call_args.kwargs["env"])

    def test_env_strips_anthropic_keys(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x",
                                          "ANTHROPIC_AUTH_TOKEN": "t"}):
            with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope())) as sr:
                claude_cli.ask_claude_cli("p", context="claude_analyst")
            env = sr.call_args.kwargs["env"]
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    # ---- process-level failures -> None -------------------------------
    def test_nonzero_exit_returns_none(self):
        with mock.patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_timeout_returns_none_and_retries(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=45)) as sr:
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))
        self.assertEqual(sr.call_count, claude_cli.CLI_MAX_ATTEMPTS)

    def test_binary_missing_returns_none(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_retry_then_success(self):
        seq = [subprocess.TimeoutExpired(cmd="claude", timeout=45),
               _proc(stdout=_envelope())]
        with mock.patch("subprocess.run", side_effect=seq) as sr:
            out = claude_cli.ask_claude_cli("p", context="claude_analyst")
        self.assertEqual(sr.call_count, 2)
        self.assertEqual(out["action"], "WAIT")

    # ---- envelope-level failures -> None ------------------------------
    def test_is_error_true_returns_none(self):
        with mock.patch("subprocess.run",
                        return_value=_proc(stdout=_envelope(result="rate limited", is_error=True))):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_empty_result_returns_none(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope(result="   "))):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_empty_stdout_returns_none(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout="")):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_envelope_not_json_returns_none(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout="not json at all")):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    # ---- result-text parsing tolerance -------------------------------
    def test_result_with_code_fences(self):
        fenced = "```json\n{\"action\": \"BUY\", \"confidence\": 61}\n```"
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope(result=fenced))):
            out = claude_cli.ask_claude_cli("p", context="claude_analyst")
        self.assertEqual(out["action"], "BUY")

    def test_result_with_prose_preamble(self):
        pre = 'Here is my analysis:\n{"action": "AVOID", "confidence": 20}\nHope that helps.'
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope(result=pre))):
            out = claude_cli.ask_claude_cli("p", context="claude_analyst")
        self.assertEqual(out["action"], "AVOID")

    def test_result_truncated_json_returns_none(self):
        with mock.patch("subprocess.run",
                        return_value=_proc(stdout=_envelope(result='{"action": "WAIT", "conf'))):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    def test_result_plain_error_text_returns_none(self):
        with mock.patch("subprocess.run",
                        return_value=_proc(stdout=_envelope(result="Error: model overloaded"))):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))

    # ---- audit logging ----------------------------------------------
    def test_logs_backend_and_cost_on_success(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope())):
            claude_cli.ask_claude_cli("p", context="claude_analyst", symbol="NABIL")
        self.assertTrue(self.mock_log.called)
        kw = self.mock_log.call_args.kwargs
        self.assertEqual(kw["backend"], "cli")
        self.assertEqual(kw["cost_usd"], "0.041")
        self.assertEqual(kw["tokens_in"], "12000")
        self.assertEqual(kw["tokens_out"], "180")

    def test_log_call_false_skips_logging(self):
        with mock.patch("subprocess.run", return_value=_proc(stdout=_envelope())):
            claude_cli.ask_claude_cli("p", context="claude_analyst", log_call=False)
        self.assertFalse(self.mock_log.called)

    def test_never_raises_on_unexpected_error(self):
        with mock.patch("subprocess.run", side_effect=RuntimeError("weird")):
            self.assertIsNone(claude_cli.ask_claude_cli("p", context="claude_analyst"))


class RouterTests(unittest.TestCase):

    def setUp(self):
        claude_router._cli_fail_streak = 0
        claude_router._cli_cooldown_until = 0.0
        self.or_mock = mock.patch("AI.claude_router._ask_claude_openrouter",
                                  return_value={"src": "openrouter"}).start()
        self.cli_mock = mock.patch("AI.claude_router._ask_claude_cli",
                                   return_value={"src": "cli"}).start()
        self.addCleanup(mock.patch.stopall)

    def _set_backend(self, value):
        return mock.patch("sheets.get_setting", return_value=value)

    def test_default_is_openrouter(self):
        with self._set_backend("openrouter"):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "openrouter")
        self.cli_mock.assert_not_called()

    def test_backend_cli(self):
        with self._set_backend("cli"):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "cli")
        self.or_mock.assert_not_called()

    def test_backend_auto_cli_serves(self):
        with self._set_backend("auto"):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "cli")
        self.or_mock.assert_not_called()

    def test_backend_auto_falls_back_to_openrouter(self):
        self.cli_mock.return_value = None
        with self._set_backend("auto"):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "openrouter")
        self.cli_mock.assert_called_once()

    def test_unknown_backend_defaults_to_openrouter(self):
        with self._set_backend("banana"):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "openrouter")

    def test_get_setting_failure_defaults_to_openrouter(self):
        with mock.patch("sheets.get_setting", side_effect=RuntimeError("db down")):
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "openrouter")

    def test_non_analyst_context_bypasses_router(self):
        # Even with cli selected, a non-analyst caller must hit OpenRouter.
        with self._set_backend("cli"):
            out = claude_router.ask_claude("p", context="learning_hub")
        self.assertEqual(out["src"], "openrouter")
        self.cli_mock.assert_not_called()

    def test_circuit_breaker_opens_after_consecutive_failures(self):
        self.cli_mock.return_value = None
        with self._set_backend("auto"):
            for _ in range(claude_router._CB_THRESHOLD):
                claude_router.ask_claude("p", context="claude_analyst")
            calls_before = self.cli_mock.call_count
            # breaker now open — next auto call must skip the CLI entirely
            out = claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(out["src"], "openrouter")
        self.assertEqual(self.cli_mock.call_count, calls_before)

    def test_circuit_breaker_resets_on_success(self):
        with self._set_backend("auto"):
            self.cli_mock.return_value = None
            claude_router.ask_claude("p", context="claude_analyst")
            self.cli_mock.return_value = {"src": "cli"}
            claude_router.ask_claude("p", context="claude_analyst")
        self.assertEqual(claude_router._cli_fail_streak, 0)

    def test_params_forwarded(self):
        with self._set_backend("cli"):
            claude_router.ask_claude("p", system="SYS", max_tokens=999,
                                     temperature=0.7, context="claude_analyst",
                                     symbol="NLIC")
        kw = self.cli_mock.call_args.kwargs
        self.assertEqual(kw["system"], "SYS")
        self.assertEqual(kw["max_tokens"], 999)
        self.assertEqual(kw["temperature"], 0.7)
        self.assertEqual(kw["symbol"], "NLIC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
