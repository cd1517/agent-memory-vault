from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_env


def load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_env_test_module", SCRIPTS / "bootstrap.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_into_child(path: Path, names: list[str]) -> dict[str, str | None]:
    probe = f"import json,os;print(json.dumps({{n:os.getenv(n) for n in {names!r}}}))"
    completed = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; exec "$2" -c "$3"',
            "sh",
            str(path),
            sys.executable,
            probe,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


class BootstrapEnvironmentTests(unittest.TestCase):
    def test_shell_export_rejects_multiline_values(self) -> None:
        bootstrap = load_bootstrap()
        with self.assertRaises(ValueError):
            bootstrap.shell_export("AGENT_MEMORY_APP_ID", "first\nsecond")

    def test_generated_env_round_trips_shell_sensitive_values_in_python(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory(prefix="agent-memory-env-") as raw_tmp:
            root = Path(raw_tmp)
            memory_root = root / "memory #1's vault"
            args = SimpleNamespace(
                config_root=str(root / "config root"),
                git_root="",
                state_db=str(root / "state db.sqlite"),
                user_id="demo user #1",
                agent_id="shared $agent",
                app_id="agent's memory",
                overwrite_env=False,
            )
            with mock.patch.object(bootstrap, "REPO_ROOT", root):
                bootstrap.write_env(args, memory_root)
            with mock.patch.object(agent_memory_env, "RUNTIME_ROOT", root):
                agent_memory_env.reset_config_cache()
                values = agent_memory_env.load_dotenv()
            agent_memory_env.reset_config_cache()

        self.assertEqual(values["AGENT_MEMORY_ROOT"], str(memory_root))
        self.assertEqual(values["AGENT_MEMORY_USER_ID"], args.user_id)
        self.assertEqual(values["AGENT_MEMORY_AGENT_ID"], args.agent_id)
        self.assertEqual(values["AGENT_MEMORY_APP_ID"], args.app_id)

    @unittest.skipIf(os.name == "nt", "POSIX source semantics are not used on Windows")
    def test_example_and_generated_env_export_to_child_processes(self) -> None:
        example_names = ["AGENT_MEMORY_ROOT", "MEMORY_ACTOR", "AGENT_MEMORY_MODEL_REVISION"]
        example = source_into_child(REPO_ROOT / ".env.example", example_names)
        self.assertEqual(example["AGENT_MEMORY_ROOT"], "/path/to/your/agent-memory-vault")
        self.assertEqual(example["MEMORY_ACTOR"], "codex")
        self.assertEqual(example["AGENT_MEMORY_MODEL_REVISION"], "")

        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory(prefix="agent-memory-shell-") as raw_tmp:
            root = Path(raw_tmp)
            memory_root = root / "memory #1's vault"
            args = SimpleNamespace(
                config_root=str(root / "config root"),
                git_root="",
                state_db=str(root / "state db.sqlite"),
                user_id="demo user #1",
                agent_id="shared $agent",
                app_id="agent's memory",
                overwrite_env=False,
            )
            with mock.patch.object(bootstrap, "REPO_ROOT", root):
                bootstrap.write_env(args, memory_root)
            names = [
                "AGENT_MEMORY_ROOT",
                "AGENT_MEMORY_USER_ID",
                "AGENT_MEMORY_AGENT_ID",
                "AGENT_MEMORY_APP_ID",
            ]
            values = source_into_child(root / ".env", names)

        self.assertEqual(values["AGENT_MEMORY_ROOT"], str(memory_root))
        self.assertEqual(values["AGENT_MEMORY_USER_ID"], args.user_id)
        self.assertEqual(values["AGENT_MEMORY_AGENT_ID"], args.agent_id)
        self.assertEqual(values["AGENT_MEMORY_APP_ID"], args.app_id)


if __name__ == "__main__":
    unittest.main()
