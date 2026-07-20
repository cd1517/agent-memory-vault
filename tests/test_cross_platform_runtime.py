from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import sys


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import agent_memory_closeout as closeout
from agent_memory_lock import try_lock, unlock


class CrossPlatformRuntimeTests(unittest.TestCase):
    def test_process_lock_serializes_two_file_handles(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            lock_path = Path(raw_root) / "locks" / "runtime.lock"
            lock_path.parent.mkdir()
            with lock_path.open("a+", encoding="utf-8") as first, lock_path.open(
                "a+", encoding="utf-8"
            ) as second:
                self.assertTrue(try_lock(first))
                self.assertFalse(try_lock(second))
                unlock(first)
                self.assertTrue(try_lock(second))
                unlock(second)

    def test_closeout_skips_zvec_when_semantic_retrieval_is_disabled(self) -> None:
        args = Namespace(skip_zvec=False, dry_run=False, zvec_timeout=5)
        with (
            mock.patch.object(closeout, "SEMANTIC_ENABLED", False),
            mock.patch.object(closeout, "run_command") as run_command,
        ):
            result = closeout.run_zvec([Path("memory.md")], args)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["detail"], "semantic_retrieval_disabled")
        run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
