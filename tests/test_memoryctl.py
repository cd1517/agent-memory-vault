from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def load_memoryctl():
    path = SCRIPTS_ROOT / "memoryctl"
    loader = importlib.machinery.SourceFileLoader("test_memoryctl_module", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class MemoryctlInterpreterTests(unittest.TestCase):
    def test_content_studio_central_allowlist_rejects_every_low_level_command(self) -> None:
        forbidden = (
            "search",
            "prewrite",
            "closeout",
            "audit",
            "audit-autorun",
            "claim",
            "claims",
            "claims-expire",
            "observe-deletion",
            "observe-committed",
            "doctor",
            "index",
            "zvec",
            "check",
            "decision-outcomes",
            "policy-benchmark",
            "intent",
        )
        for command_name in forbidden:
            module = load_memoryctl()
            with (
                self.subTest(command=command_name),
                mock.patch.object(
                    sys,
                    "argv",
                    ["memoryctl", "--actor", "yichen-content-studio", command_name],
                ),
                mock.patch.object(module.subprocess, "run") as invoked,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(module.main(), 2)
                invoked.assert_not_called()

    def test_direct_studio_intent_create_and_no_approval_bypass_are_rejected(self) -> None:
        environment = os.environ.copy()
        environment["AGENT_MEMORY_SESSION_ID"] = "direct-studio-bypass-session"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "agent_memory_intent.py"),
                "--actor",
                "yichen-content-studio",
                "--json",
                "create",
                "--target",
                "/definitely/not/a/studio/target.md",
                "--proposal-file",
                "/definitely/not/a/studio/proposal.md",
                "--no-approval-required",
                "--source-class",
                "user_direct",
                "--knowledge-kind",
                "fact",
                "--asserted-by",
                "user",
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["reason_code"], "STUDIO_LOW_LEVEL_API_FORBIDDEN")

    def test_content_studio_write_uses_only_the_plugin_session(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        environment = {
            "AGENT_MEMORY_SESSION_ID": "content-studio-session",
            "CODEX_THREAD_ID": "outer-codex-session",
        }
        with (
            mock.patch.dict(module.os.environ, environment, clear=True),
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "yichen-content-studio",
                    "write",
                    "prepare",
                    "--json",
                ],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertTrue(str(command[1]).endswith("agent_memory_write.py"))
        self.assertEqual(command[2:4], ["--actor", "yichen-content-studio"])
        self.assertNotIn("--session-id", command)
        self.assertNotIn("content-studio-session", command)
        self.assertNotIn("outer-codex-session", command)
        self.assertIn("prepare", command)
        child_env = invoked.call_args.kwargs["env"]
        self.assertEqual(child_env["AGENT_MEMORY_SESSION_ID"], "content-studio-session")

    def test_content_studio_write_rejects_every_explicit_session_spelling(self) -> None:
        private_marker = "private-session-argv-probe-94731"
        variants = (
            ["--session-id", private_marker],
            [f"--session-id={private_marker}"],
            ["--session", private_marker],
            ["--sess", private_marker],
        )
        for variant in variants:
            module = load_memoryctl()
            stderr = io.StringIO()
            with (
                self.subTest(variant=variant[0]),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "memoryctl",
                        "--actor",
                        "yichen-content-studio",
                        "write",
                        "read-target",
                        *variant,
                    ],
                ),
                mock.patch.object(module.subprocess, "run") as invoked,
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(module.main(), 2)
            invoked.assert_not_called()
            self.assertNotIn(private_marker, stderr.getvalue())

    def test_content_studio_retrieve_forwards_distinct_actor(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "yichen-content-studio",
                    "retrieve",
                    "--json",
                ],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertTrue(str(command[1]).endswith("agent_memory_retrieve.py"))
        self.assertEqual(command[2:4], ["--actor", "yichen-content-studio"])
        self.assertEqual(command[4:], ["--json"])
        self.assertNotIn("project query", command)

    def test_content_studio_prewrite_requires_a_factual_asserter(self) -> None:
        module = load_memoryctl()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "yichen-content-studio", "prewrite", "summary"],
            ),
            mock.patch.object(module.subprocess, "run") as invoked,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(module.main(), 2)
        invoked.assert_not_called()
        self.assertIn("may use only retrieve, write, or version", stderr.getvalue())

    def test_content_studio_prewrite_accepts_named_model_asserter(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "yichen-content-studio",
                    "prewrite",
                    "summary",
                    "--asserted-by",
                    "opencode",
                ],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 2)
        invoked.assert_not_called()

    def test_content_studio_closeout_without_plugin_session_fails_closed(self) -> None:
        module = load_memoryctl()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "yichen-content-studio", "closeout", "--dry-run"],
            ),
            mock.patch.object(module, "host_session_id", return_value=""),
            mock.patch.object(module.subprocess, "run") as invoked,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(module.main(), 2)
        invoked.assert_not_called()
        self.assertIn("may use only retrieve, write, or version", stderr.getvalue())

    def test_content_studio_prewrite_rejects_ambiguous_duplicate_asserters(self) -> None:
        module = load_memoryctl()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "yichen-content-studio",
                    "prewrite",
                    "summary",
                    "--asserted-by",
                    "user",
                    "--asserted-by",
                    "codex",
                ],
            ),
            mock.patch.object(module.subprocess, "run") as invoked,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(module.main(), 2)
        invoked.assert_not_called()

    def test_core_command_uses_current_python(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(sys, "argv", ["memoryctl", "--actor", "human", "doctor", "--json"]),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(str(command[1]).endswith("agent_memory_doctor.py"))
        self.assertEqual(command[2:], ["--json"])

    def test_zvec_uses_configured_semantic_python(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)

        def configured(name: str, default: str = "") -> str:
            return "/configured/semantic/python" if name == "ZVEC_PYTHON" else default

        with (
            mock.patch.object(sys, "argv", ["memoryctl", "--actor", "codex", "zvec", "--report"]),
            mock.patch.object(module, "env_value", side_effect=configured),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertEqual(command[0], "/configured/semantic/python")
        self.assertTrue(str(command[1]).endswith("agent_memory_zvec_index.py"))
        self.assertEqual(command[2:], ["--report"])

    def test_agent_closeout_without_session_fails_closed(self) -> None:
        module = load_memoryctl()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "claude", "closeout", "--dry-run"],
            ),
            mock.patch.object(module, "host_session_id", return_value=""),
            mock.patch.object(module.subprocess, "run") as invoked,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(module.main(), 2)

        invoked.assert_not_called()
        self.assertIn("requires an active host session", stderr.getvalue())

    def test_explicit_session_closeout_is_always_claim_scoped(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "claude",
                    "closeout",
                    "--dry-run",
                    "--session-id",
                    "explicit-session",
                ],
            ),
            mock.patch.object(module, "host_session_id", return_value=""),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertIn("--session-id", command)
        self.assertIn("explicit-session", command)
        self.assertIn("--claimed-only", command)

    def test_global_closeout_requires_explicit_global_flag(self) -> None:
        module = load_memoryctl()
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "claude", "closeout", "--global", "--dry-run"],
            ),
            mock.patch.object(module, "host_session_id", return_value=""),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)

        command = invoked.call_args.args[0]
        self.assertNotIn("--claimed-only", command)


if __name__ == "__main__":
    unittest.main()
