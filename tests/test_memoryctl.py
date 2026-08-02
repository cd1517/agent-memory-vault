from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
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
