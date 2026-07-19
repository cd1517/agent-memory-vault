from __future__ import annotations

import subprocess
import sys
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import agent_memory_search as search
import agent_memory_zvec_index as zvec_index


class SearchPythonTests(unittest.TestCase):
    def test_zvec_search_uses_configured_python(self) -> None:
        args = Namespace(
            no_zvec=False,
            query="semantic query",
            limit=3,
            zvec_timeout=5,
            zvec_max_distance=0.8,
        )
        completed = subprocess.CompletedProcess([], 0, stdout='{"results": []}', stderr="")
        with mock.patch.object(search, "ZVEC_PYTHON", "/custom/vector/python"):
            with mock.patch.object(search.subprocess, "run", return_value=completed) as run:
                results, warnings = search.zvec_search(args)

        self.assertEqual(results, [])
        self.assertEqual(warnings, [])
        self.assertEqual(run.call_args.args[0][0], "/custom/vector/python")
        self.assertEqual(run.call_args.args[0][1], str(search.ZVEC_SCRIPT))

    def test_zvec_cli_serializes_readers_and_writers(self) -> None:
        args = Namespace(
            init=False,
            scan=False,
            prune=False,
            report=False,
            changed_file=[],
            search="healthcheck",
            lock_timeout=7.0,
        )
        calls: list[tuple[bool, float]] = []

        @contextmanager
        def recorded_lock(*, exclusive: bool, timeout: float):
            calls.append((exclusive, timeout))
            yield

        with (
            mock.patch.object(zvec_index, "parse_args", return_value=args),
            mock.patch.object(zvec_index, "zvec_lock", side_effect=recorded_lock),
            mock.patch.object(zvec_index, "run_locked", return_value=0),
        ):
            self.assertEqual(zvec_index.main(), 0)

        self.assertEqual(calls, [(True, 7.0)])


if __name__ == "__main__":
    unittest.main()
