#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_memory_env import env_value, expand_path
from agent_memory_lock import try_lock, unlock
import agent_memory_intent as write_intent
from agent_memory_state import absolute_path, secure_sqlite_connect


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = expand_path(env_value("ROOT", str(RUNTIME_ROOT / "templates" / "vault"))).resolve()
STATE_DB = absolute_path(expand_path(env_value("STATE_DB", "$HOME/.config/agent-memory/state.sqlite")))
CONFIG_ROOT = expand_path(env_value("CONFIG_ROOT", "$HOME/.config/agent-memory")).resolve()


def find_default_git_root() -> Path:
    for candidate in (VAULT_ROOT, *VAULT_ROOT.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return VAULT_ROOT.resolve()


GIT_ROOT = expand_path(env_value("GIT_ROOT", str(find_default_git_root()))).resolve()
FORMAL_MEMORY_TOP_LEVELS = {"用户记忆", "项目", "工作流", "决策", "agent"}
FORMAL_TOP_LEVEL_FILES = {"AGENTS.md", "INDEX.md", "README.md", "STRUCTURE.md"}
DELETED_OBSERVATION_PREFIX = "deleted:"
DELETED_OBSERVATION_RE = re.compile(r"^deleted:([0-9a-f]{40}):([0-9a-f]{64})$")
DELETION_OBSERVATION_LOCK = CONFIG_ROOT / "locks" / "closeout.lock"
ACTOR_SESSION_ENV_KEYS = {
    "codex": ("AGENT_MEMORY_SESSION_ID", "CODEX_THREAD_ID"),
    "claude": ("AGENT_MEMORY_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
    "human": ("AGENT_MEMORY_SESSION_ID",),
    "migration": ("AGENT_MEMORY_SESSION_ID",),
    "test": ("AGENT_MEMORY_SESSION_ID",),
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def deleted_observation_sentinel(deletion_commit: str, prior_sha256: str) -> str:
    commit = deletion_commit.strip().lower()
    digest = prior_sha256.strip().lower()
    value = f"{DELETED_OBSERVATION_PREFIX}{commit}:{digest}"
    if parse_deleted_observation(value) is None:
        raise ValueError("deleted observation requires a 40-hex commit and 64-hex prior SHA-256")
    return value


def parse_deleted_observation(value: str) -> tuple[str, str] | None:
    """Parse a deletion sentinel without consulting Git or SQLite."""

    match = DELETED_OBSERVATION_RE.fullmatch(str(value or ""))
    if match is None:
        return None
    return match.group(1), match.group(2)


def session_value(explicit: str = "", actor: str = "codex") -> str:
    if explicit.strip():
        return explicit.strip()
    for key in ACTOR_SESSION_ENV_KEYS.get(actor, ("AGENT_MEMORY_SESSION_ID",)):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def session_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    conn = secure_sqlite_connect(
        STATE_DB,
        timeout=10,
        create=not read_only,
        read_only=read_only,
        row_factory=sqlite3.Row,
        pragmas=("PRAGMA busy_timeout=10000",),
    )
    if not read_only:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_session_claims (
          session_hash TEXT NOT NULL,
          actor TEXT NOT NULL,
          path TEXT NOT NULL,
          rel_path TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          claimed_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          intent_id TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (session_hash, path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_file_observations (
          path TEXT PRIMARY KEY,
          rel_path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          actor TEXT NOT NULL,
          session_hash TEXT NOT NULL DEFAULT '',
          observed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_deletion_observations (
          observation_id TEXT PRIMARY KEY,
          path TEXT NOT NULL,
          rel_path TEXT NOT NULL,
          sentinel TEXT NOT NULL,
          actor TEXT NOT NULL,
          user_authorized INTEGER NOT NULL,
          deletion_commit TEXT NOT NULL,
          parent_commit TEXT NOT NULL,
          prior_sha256 TEXT NOT NULL,
          trash_sha256 TEXT NOT NULL,
          trash_path_sha256 TEXT NOT NULL,
          evidence_ref_sha256 TEXT NOT NULL,
          evidence_ref_length INTEGER NOT NULL,
          observed_at TEXT NOT NULL,
          UNIQUE(path, deletion_commit)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_session_claims_active "
        "ON memory_session_claims(status, actor, session_hash)"
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_session_claims)")}
    if "intent_id" not in columns:
        conn.execute("ALTER TABLE memory_session_claims ADD COLUMN intent_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_session_claims_active_intent "
        "ON memory_session_claims(intent_id) WHERE intent_id<>'' AND status='active'"
    )
    write_intent.ensure_schema(conn)
    conn.commit()


def record_file_observations(raw_session_id: str, actor: str, paths: list[Path]) -> int:
    rows: list[tuple[str, str, str]] = []
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        try:
            rel_path = path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            continue
        rows.append((str(path), rel_path, file_sha256(path)))
    if not rows:
        return 0
    now = utc_now()
    hashed = session_hash(raw_session_id)
    with connect() as conn:
        for path, rel_path, digest in rows:
            conn.execute(
                """
                INSERT INTO memory_file_observations (
                  path, rel_path, sha256, actor, session_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  rel_path=excluded.rel_path,
                  sha256=excluded.sha256,
                  actor=excluded.actor,
                  session_hash=excluded.session_hash,
                  observed_at=excluded.observed_at
                """,
                (path, rel_path, digest, actor, hashed, now),
            )
        conn.commit()
    return len(rows)


def normalize_claim_path(raw: str, *, allow_missing: bool = False) -> tuple[Path, str]:
    if allow_missing:
        target = write_intent.canonical_target(raw)
        if target.path.exists() and not target.path.is_file():
            raise ValueError(f"claim path is not a regular file: {target.path}")
        if not target.path.parent.is_dir():
            raise ValueError(f"claim parent directory does not exist: {target.path.parent}")
        return target.path, target.rel_path
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    try:
        rel_path = path.relative_to(VAULT_ROOT).as_posix()
    except ValueError as exc:
        raise ValueError(f"claim path is outside the memory vault: {path}") from exc
    if path.suffix.lower() != ".md":
        raise ValueError(f"claim path is not Markdown: {path}")
    if not path.exists():
        raise ValueError(f"claim path does not exist: {path}")
    return path, rel_path


def _is_formal_memory_markdown(rel_path: Path) -> bool:
    if rel_path.suffix.lower() != ".md":
        return False
    if len(rel_path.parts) == 1:
        return rel_path.name in FORMAL_TOP_LEVEL_FILES
    return bool(rel_path.parts) and rel_path.parts[0] in FORMAL_MEMORY_TOP_LEVELS


def _normalize_missing_formal_path(raw: str) -> tuple[Path, str, str]:
    path = Path(raw).expanduser()
    try:
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve(strict=False)
        else:
            path = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("deletion target path could not be resolved") from exc
    try:
        relative = path.relative_to(VAULT_ROOT)
    except ValueError as exc:
        raise ValueError("deletion target is outside the memory vault") from exc
    if not _is_formal_memory_markdown(relative):
        raise ValueError("deletion target is not formal vault Markdown")
    if any(character in relative.as_posix() for character in ("\0", "\n", "\r", "\t")):
        raise ValueError("deletion target contains unsupported control characters")
    if os.path.lexists(path):
        raise ValueError("deletion target still exists")
    if not path.parent.is_dir():
        raise ValueError("deletion target parent directory does not exist")
    try:
        repo_path = path.relative_to(GIT_ROOT).as_posix()
    except ValueError as exc:
        raise ValueError("memory vault is outside the configured Git root") from exc
    return path, relative.as_posix(), repo_path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_recognized_trash_path(path: Path) -> bool:
    """Accept only platform Trash roots, never a lookalike path component."""

    home_trash = (Path.home() / ".Trash").resolve(strict=False)
    if _path_is_within(path, home_trash):
        return True

    xdg_data_home = Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    ).expanduser().resolve(strict=False)
    if _path_is_within(path, xdg_data_home / "Trash" / "files"):
        return True

    if os.name == "nt":
        recycle_root = Path(path.anchor) / "$Recycle.Bin"
        return bool(path.anchor) and _path_is_within(path, recycle_root)

    if hasattr(os, "getuid"):
        uid = str(os.getuid())
        try:
            volume_relative = path.relative_to(Path("/Volumes"))
        except ValueError:
            volume_relative = None
        if volume_relative is not None:
            parts = volume_relative.parts
            if len(parts) >= 4 and parts[1:3] == (".Trashes", uid):
                return True
    return False


def _normalize_trash_file(raw: str) -> Path:
    lexical_path = Path(raw).expanduser()
    if not lexical_path.is_absolute():
        raise ValueError("Trash path must be absolute")
    if lexical_path.is_symlink():
        raise ValueError("Trash evidence is not an existing regular file")
    try:
        path = lexical_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Trash evidence is not an existing regular file") from exc
    if not _is_recognized_trash_path(path):
        raise ValueError("provided path is not inside a recognized Trash location")
    if not path.is_file():
        raise ValueError("Trash evidence is not an existing regular file")
    return path


def _run_git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(GIT_ROOT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git validation could not be completed") from exc


def _require_clean_git_path(repo_path: str) -> None:
    result = _run_git(
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        repo_path,
    )
    if result.returncode != 0:
        raise ValueError("target Git state could not be verified")
    if result.stdout:
        raise ValueError("deletion target has uncommitted Git index or worktree state")


def _resolved_commit(raw_commit: str) -> str:
    candidate = raw_commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", candidate):
        raise ValueError("deletion commit must be a hexadecimal Git commit id")
    result = _run_git("rev-parse", "--verify", f"{candidate}^{{commit}}")
    resolved = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError("deletion commit cannot be resolved")
    return resolved


def _deletion_parent_and_prior_sha(deletion_commit: str, repo_path: str) -> tuple[str, str]:
    parents_result = _run_git("rev-list", "--parents", "-n", "1", deletion_commit)
    tokens = parents_result.stdout.decode("ascii", errors="ignore").strip().lower().split()
    if parents_result.returncode != 0 or not tokens or tokens[0] != deletion_commit or len(tokens) < 2:
        raise ValueError("deletion commit has no verifiable parent")

    commit_path = _run_git("cat-file", "-e", f"{deletion_commit}:{repo_path}")
    if commit_path.returncode == 0:
        raise ValueError("deletion commit still contains the target path")

    for parent_commit in tokens[1:]:
        status_result = _run_git(
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-renames",
            "--name-status",
            "-z",
            parent_commit,
            deletion_commit,
            "--",
            repo_path,
        )
        status_parts = [part for part in status_result.stdout.split(b"\0") if part]
        if status_result.returncode != 0 or len(status_parts) < 2:
            continue
        if status_parts[0] != b"D" or status_parts[1].decode("utf-8", errors="strict") != repo_path:
            continue
        blob_result = _run_git("rev-parse", "--verify", f"{parent_commit}:{repo_path}")
        blob_oid = blob_result.stdout.decode("ascii", errors="ignore").strip().lower()
        if blob_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", blob_oid):
            continue
        content_result = _run_git("cat-file", "blob", blob_oid)
        if content_result.returncode != 0:
            continue
        return parent_commit, hashlib.sha256(content_result.stdout).hexdigest()
    raise ValueError("provided commit did not delete the target relative to a parent")


def validate_deletion_observation(
    *,
    actor: str,
    target_file: str,
    trash_file: str,
    deletion_commit: str,
    evidence_ref: str,
    user_authorized: bool,
) -> dict[str, Any]:
    if actor != "human":
        raise ValueError("deletion observations are restricted to actor=human")
    if not user_authorized:
        raise ValueError("explicit user authorization flag is required")
    evidence = evidence_ref.strip()
    if not evidence:
        raise ValueError("evidence ref is required")
    if len(evidence) > 4096:
        raise ValueError("evidence ref is too long")

    target, rel_path, repo_path = _normalize_missing_formal_path(target_file)
    trash = _normalize_trash_file(trash_file)
    _require_clean_git_path(repo_path)
    resolved_commit = _resolved_commit(deletion_commit)
    ancestor = _run_git("merge-base", "--is-ancestor", resolved_commit, "HEAD")
    if ancestor.returncode == 1:
        raise ValueError("deletion commit is not an ancestor of current HEAD")
    if ancestor.returncode != 0:
        raise ValueError("deletion commit ancestry could not be verified")

    parent_commit, prior_sha256 = _deletion_parent_and_prior_sha(resolved_commit, repo_path)
    latest_result = _run_git("log", "-1", "--format=%H", "HEAD", "--", repo_path)
    latest_commit = latest_result.stdout.decode("ascii", errors="ignore").strip().lower()
    if latest_result.returncode != 0 or latest_commit != resolved_commit:
        raise ValueError("deletion commit is not the target path's latest change")
    try:
        trash_sha256 = file_sha256(trash)
    except OSError as exc:
        raise ValueError("Trash evidence could not be read") from exc
    if trash_sha256 != prior_sha256:
        raise ValueError("Trash evidence SHA-256 does not match the pre-deletion Git blob")

    evidence_sha256 = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    trash_path_sha256 = hashlib.sha256(str(trash).encode("utf-8")).hexdigest()
    sentinel = deleted_observation_sentinel(resolved_commit, prior_sha256)
    observation_material = "\0".join(
        (str(target), sentinel, trash_path_sha256, evidence_sha256, "explicit_user")
    )
    return {
        "observation_id": hashlib.sha256(observation_material.encode("utf-8")).hexdigest(),
        "path": str(target),
        "rel_path": rel_path,
        "sentinel": sentinel,
        "actor": actor,
        "user_authorized": 1,
        "deletion_commit": resolved_commit,
        "parent_commit": parent_commit,
        "prior_sha256": prior_sha256,
        "trash_sha256": trash_sha256,
        "trash_path_sha256": trash_path_sha256,
        "evidence_ref_sha256": evidence_sha256,
        "evidence_ref_length": len(evidence),
    }


@contextlib.contextmanager
def deletion_observation_lock(timeout: float = 15.0):
    DELETION_OBSERVATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with DELETION_OBSERVATION_LOCK.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                if try_lock(handle):
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("another memory closeout or deletion observation is still running")
            time.sleep(0.1)
        try:
            yield
        finally:
            unlock(handle)


def _store_deletion_observation(observation: dict[str, Any]) -> int:
    now = utc_now()
    audit_columns = (
        "observation_id",
        "path",
        "rel_path",
        "sentinel",
        "actor",
        "user_authorized",
        "deletion_commit",
        "parent_commit",
        "prior_sha256",
        "trash_sha256",
        "trash_path_sha256",
        "evidence_ref_sha256",
        "evidence_ref_length",
    )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT observation_id, path, rel_path, sentinel, actor, user_authorized,
                   deletion_commit, parent_commit, prior_sha256, trash_sha256,
                   trash_path_sha256, evidence_ref_sha256, evidence_ref_length
            FROM memory_deletion_observations
            WHERE path=? AND deletion_commit=?
            """,
            (observation["path"], observation["deletion_commit"]),
        ).fetchone()
        expected = tuple(observation[column] for column in audit_columns)
        if existing is not None:
            actual = tuple(existing[column] for column in audit_columns)
            if actual != expected:
                conn.rollback()
                raise ValueError("existing deletion audit record does not match this evidence")
            current = conn.execute(
                "SELECT sha256 FROM memory_file_observations WHERE path=?",
                (observation["path"],),
            ).fetchone()
            if current is not None and str(current[0]) == observation["sentinel"]:
                conn.rollback()
                return 0
        else:
            conn.execute(
                """
                INSERT INTO memory_deletion_observations (
                  observation_id, path, rel_path, sentinel, actor, user_authorized,
                  deletion_commit, parent_commit, prior_sha256, trash_sha256,
                  trash_path_sha256, evidence_ref_sha256, evidence_ref_length, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected, now),
            )
        conn.execute(
            """
            INSERT INTO memory_file_observations (
              path, rel_path, sha256, actor, session_hash, observed_at
            ) VALUES (?, ?, ?, ?, '', ?)
            ON CONFLICT(path) DO UPDATE SET
              rel_path=excluded.rel_path,
              sha256=excluded.sha256,
              actor=excluded.actor,
              session_hash='',
              observed_at=excluded.observed_at
            """,
            (
                observation["path"],
                observation["rel_path"],
                observation["sentinel"],
                observation["actor"],
                now,
            ),
        )
        conn.commit()
    return 1


def apply_deletion_observation(
    observation: dict[str, Any],
    *,
    actor: str,
    target_file: str,
    trash_file: str,
    deletion_commit: str,
    evidence_ref: str,
    user_authorized: bool,
) -> int:
    with deletion_observation_lock():
        refreshed = validate_deletion_observation(
            actor=actor,
            target_file=target_file,
            trash_file=trash_file,
            deletion_commit=deletion_commit,
            evidence_ref=evidence_ref,
            user_authorized=user_authorized,
        )
        if refreshed != observation:
            raise ValueError("deletion evidence changed between preview and apply")
        return _store_deletion_observation(refreshed)


def safe_deletion_observation_payload(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: observation[key]
        for key in (
            "rel_path",
            "sentinel",
            "actor",
            "user_authorized",
            "deletion_commit",
            "parent_commit",
            "prior_sha256",
            "trash_sha256",
            "trash_path_sha256",
            "evidence_ref_sha256",
            "evidence_ref_length",
        )
    }


def claim_paths(actor: str, raw_session_id: str, paths: list[str], intent_id: str = "") -> list[dict[str, str]]:
    hashed = session_hash(raw_session_id)
    if not hashed:
        raise ValueError("session id is required; pass --session-id or use a supported host session environment")
    normalized = [normalize_claim_path(raw, allow_missing=bool(intent_id)) for raw in paths]
    if intent_id and len(normalized) != 1:
        raise ValueError("one write intent can bind exactly one claimed file")
    for path, rel_path in normalized:
        if (
            write_intent.PROTECTED_PATHS
            and write_intent.ENFORCEMENT_MODE == "enforce"
            and write_intent.is_protected_target(path)
            and not intent_id
        ):
            raise ValueError(f"protected memory requires a bound write intent before editing: {rel_path}")
    now = utc_now()
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if intent_id:
                bound = write_intent.bind_claim(
                    intent_id,
                    actor=actor,
                    raw_session_id=raw_session_id,
                    claim_path=normalized[0][0],
                    claim_ref=f"{actor}:{hashed}:{normalized[0][1]}",
                    connection=conn,
                )
                if str(bound.get("target_key", "")) != write_intent.canonical_target(normalized[0][0]).target_key:
                    raise ValueError("write intent target does not match claimed file")
            for path, rel_path in normalized:
                existing = conn.execute(
                    "SELECT status, intent_id FROM memory_session_claims WHERE session_hash=? AND path=?",
                    (hashed, str(path)),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing[0]) == "active"
                    and str(existing[1] or "")
                    and str(existing[1]) != intent_id
                ):
                    raise ValueError(f"active claim already has a different write intent: {rel_path}")
                conn.execute(
                    """
                    INSERT INTO memory_session_claims (
                      session_hash, actor, path, rel_path, status, claimed_at, updated_at, completed_at, intent_id
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, ?)
                    ON CONFLICT(session_hash, path) DO UPDATE SET
                      actor=excluded.actor,
                      rel_path=excluded.rel_path,
                      status='active',
                      updated_at=excluded.updated_at,
                      completed_at=NULL,
                      intent_id=CASE
                        WHEN memory_session_claims.status='active'
                             AND memory_session_claims.intent_id<>''
                             AND excluded.intent_id=''
                        THEN memory_session_claims.intent_id
                        ELSE excluded.intent_id
                      END
                    """,
                    (hashed, actor, str(path), rel_path, now, now, intent_id),
                )
            conn.commit()
    except write_intent.IntentError as exc:
        if intent_id and exc.reason_code in {"STALE_BASE", "INTENT_EXPIRED"}:
            try:
                write_intent.finalize_receipt(
                    intent_id,
                    actor=actor,
                    raw_session_id=raw_session_id,
                    outcome="expired" if exc.reason_code == "INTENT_EXPIRED" else "failed",
                    reason_code=exc.reason_code,
                    detail_code="CLAIM_BINDING_REJECTED",
                )
            except (write_intent.IntentError, OSError, sqlite3.Error):
                pass
        raise
    return [{"path": str(path), "rel_path": rel_path, "intent_id": intent_id} for path, rel_path in normalized]


def active_claim_rows(
    raw_session_id: str,
    actor: str = "",
    *,
    read_only: bool = False,
) -> list[dict[str, str]]:
    hashed = session_hash(raw_session_id)
    if not hashed:
        return []
    params: list[str] = [hashed]
    try:
        with connect(read_only=read_only) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_session_claims)")}
            if not columns:
                return []
            intent_expression = "intent_id" if "intent_id" in columns else "'' AS intent_id"
            query = (
                "SELECT session_hash, actor, path, rel_path, status, claimed_at, updated_at, "
                f"{intent_expression} FROM memory_session_claims "
                "WHERE session_hash=? AND status='active'"
            )
            if actor:
                query += " AND actor=?"
                params.append(actor)
            query += " ORDER BY rel_path"
            rows = conn.execute(query, params).fetchall()
    except (OSError, sqlite3.Error):
        if read_only and not STATE_DB.exists():
            return []
        raise
    return [{key: str(row[key] or "") for key in row.keys()} for row in rows]


def parsed_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def all_active_claim_rows(max_age_hours: float | None = None) -> list[dict[str, str]]:
    if max_age_hours is not None and max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT session_hash, actor, path, rel_path, status, claimed_at, updated_at, intent_id
            FROM memory_session_claims
            WHERE status='active'
            ORDER BY actor, session_hash, rel_path
            """
        ).fetchall()
    payloads = [{key: str(row[key] or "") for key in row.keys()} for row in rows]
    if max_age_hours is None:
        return payloads
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    return [row for row in payloads if (parsed_time(row["updated_at"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= cutoff]


def stale_active_claim_rows(max_age_hours: float = 24) -> list[dict[str, str]]:
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    rows = all_active_claim_rows()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    return [row for row in rows if (parsed_time(row["updated_at"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) < cutoff]


def expire_stale_claims(max_age_hours: float = 24, apply: bool = False) -> tuple[list[dict[str, str]], int]:
    rows = stale_active_claim_rows(max_age_hours)
    if not apply or not rows:
        return rows, 0
    now = utc_now()
    changed = 0
    with connect() as conn:
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE memory_session_claims
                SET status='expired', completed_at=?, updated_at=?
                WHERE session_hash=? AND path=? AND status='active' AND updated_at=?
                """,
                (now, now, row["session_hash"], row["path"], row["updated_at"]),
            )
            changed += int(cursor.rowcount)
        conn.commit()
    return rows, changed


def complete_claim_paths(raw_session_id: str, actor: str, paths: list[Path]) -> int:
    hashed = session_hash(raw_session_id)
    if not hashed or not paths:
        return 0
    now = utc_now()
    with connect() as conn:
        placeholders = ",".join("?" for _ in paths)
        params: list[str] = [now, now, hashed, actor, *(str(path.resolve()) for path in paths)]
        cursor = conn.execute(
            f"""
            UPDATE memory_session_claims
            SET status='completed', completed_at=?, updated_at=?
            WHERE session_hash=? AND actor=? AND status='active'
              AND path IN ({placeholders})
            """,
            params,
        )
        conn.commit()
        return int(cursor.rowcount)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track per-session ownership of shared memory files.")
    parser.add_argument("--actor", choices=("codex", "claude", "human", "migration", "test"), default="codex")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    claim_parser = subparsers.add_parser("claim", help="Claim one or more Markdown files for this session.")
    claim_parser.add_argument("--file", action="append", required=True)
    claim_parser.add_argument("--intent-id", default="", help="Bind this single-file claim to a prepared write intent.")
    subparsers.add_parser("list", help="List active claims for this session.")
    subparsers.add_parser("list-all", help="List all active claims.")
    expire_parser = subparsers.add_parser("expire-stale", help="Preview or expire abandoned active claims.")
    expire_parser.add_argument("--older-than-hours", type=float, default=24)
    expire_parser.add_argument("--apply", action="store_true", help="Mark matching claims expired; default is preview only.")
    deletion_parser = subparsers.add_parser(
        "observe-deletion",
        help="Preview or record an explicitly authorized, recoverable historical Markdown deletion.",
    )
    deletion_parser.add_argument("--file", required=True, help="Missing formal Markdown path inside the vault.")
    deletion_parser.add_argument("--trash-path", required=True, help="Existing recoverable copy in a Trash location.")
    deletion_parser.add_argument("--deletion-commit", required=True, help="Git commit that deleted the target path.")
    deletion_parser.add_argument("--evidence-ref", required=True, help="Authorization evidence; only its hash is stored.")
    deletion_parser.add_argument(
        "--confirm-user-authorized",
        action="store_true",
        help="Confirm that the user explicitly authorized this exact deletion.",
    )
    deletion_parser.add_argument("--apply", action="store_true", help="Write the audit and tombstone; default is preview only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_session_id = session_value(args.session_id, args.actor)
    applied = 0
    observation: dict[str, Any] | None = None
    try:
        if args.action == "claim":
            rows = claim_paths(args.actor, raw_session_id, args.file, args.intent_id)
        elif args.action == "list-all":
            rows = all_active_claim_rows()
        elif args.action == "expire-stale":
            rows, applied = expire_stale_claims(args.older_than_hours, args.apply)
        elif args.action == "observe-deletion":
            observation = validate_deletion_observation(
                actor=args.actor,
                target_file=args.file,
                trash_file=args.trash_path,
                deletion_commit=args.deletion_commit,
                evidence_ref=args.evidence_ref,
                user_authorized=args.confirm_user_authorized,
            )
            applied = (
                apply_deletion_observation(
                    observation,
                    actor=args.actor,
                    target_file=args.file,
                    trash_file=args.trash_path,
                    deletion_commit=args.deletion_commit,
                    evidence_ref=args.evidence_ref,
                    user_authorized=args.confirm_user_authorized,
                )
                if args.apply
                else 0
            )
            rows = []
        else:
            if not raw_session_id:
                raise ValueError("session id is required; pass --session-id or use a supported host session environment")
            rows = active_claim_rows(raw_session_id, args.actor)
    except (ValueError, OSError, sqlite3.Error) as exc:
        payload: dict[str, Any] = {"ok": False, "error": str(exc), "action": args.action}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"claim_error={exc}")
        return 2
    payload = {
        "ok": True,
        "action": args.action,
        "actor": args.actor,
        "session_hash": session_hash(raw_session_id),
        "count": len(rows),
        "claims": rows,
        "applied": applied,
    }
    if observation is not None:
        payload["preview"] = not args.apply
        payload["observation"] = safe_deletion_observation_payload(observation)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if observation is not None:
            safe = safe_deletion_observation_payload(observation)
            print(
                f"deletion_observation=ok applied={applied} preview={not args.apply} "
                f"actor={args.actor} rel_path={safe['rel_path']}"
            )
            print(f"sentinel={safe['sentinel']}")
        else:
            print(f"claims={len(rows)} applied={applied} actor={args.actor} session={payload['session_hash']}")
            for row in rows:
                print(row.get("rel_path", row.get("path", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
