from __future__ import annotations

import importlib.machinery
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
