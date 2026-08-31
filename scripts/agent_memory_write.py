#!/usr/bin/env python3
"""User-confirmed, two-phase formal Agent Memory writes for host applications.

The request body is accepted only on stdin.  Prepare performs source safety,
read-only reconciliation, target selection, and immutable intent creation; it
never changes Markdown.  Apply accepts the exact proposal again, binds an
explicit user confirmation, revalidates the baseline, claims the target,
writes the exact bytes, and runs session-scoped closeout.

Mutating actions return only bounded metadata, hashes, relative paths, and safe
reason codes.  The explicit read-target action returns one bounded current
Markdown file to the host but never logs it.  Proposal Markdown, search
excerpts, diffs, evidence text, and confirmation text are never printed by the
mutating actions or written to persistent logs.  The content-studio path also
disables the optional private proposal snapshot in the intent database,
leaving only its hashes and byte/line counts.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from agent_memory_env import env_value, expand_path
from agent_memory_lock import try_lock, unlock
from agent_memory_state import (
    POSIX_PERMISSION_MODEL,
    PRIVATE_FILE_MODE,
    StateSecurityError,
    ensure_private_directory,
)
import agent_memory_claim as memory_claim
import agent_memory_closeout as memory_closeout
import agent_memory_intent as write_intent
import agent_memory_retrieve as memory_retrieve
import agent_memory_safety as memory_safety


ACTOR = "yichen-content-studio"
ASSERTED_BY_VALUES = {"user", "claude", "codex", "opencode"}
WRITABLE_ACTIONS = {"ADD", "UPDATE"}
FORMAL_MEMORY_TOP_LEVELS = {"用户记忆", "项目", "工作流", "决策", "agent"}
SCHEMA_VERSION = 1
MAX_SUMMARY_CHARS = 2_400
MAX_REFERENCE_CHARS = 512
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_HOST_TARGET_BYTES = 2 * 1024 * 1024
CONFIG_ROOT = expand_path(env_value("CONFIG_ROOT", "$HOME/.config/agent-memory"))
WRITE_LOCK_PATH = CONFIG_ROOT / "locks" / "content-studio-write.lock"
CLOSEOUT_SCRIPT = Path(__file__).resolve().parent / "agent_memory_closeout.py"
PYTHON = env_value("PYTHON", sys.executable)
SAFE_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,511}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PROPOSAL_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
READ_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")


class StudioWriteError(ValueError):
    """A bounded protocol error that is safe to return to the host UI."""

    def __init__(self, reason_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.reason_code = _safe_code(reason_code, default="WRITE_PROTOCOL_ERROR")
        self.safe_message = message[:240]
        self.retryable = retryable


def utc_now() -> str:
    return memory_closeout.utc_now()


def _safe_code(value: str, *, default: str = "WRITE_PROTOCOL_ERROR") -> str:
    normalized = str(value).strip().upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]{0,95}", normalized):
        return normalized
    return default


def _error_message(reason_code: str) -> str:
    messages = {
        "ACTIVE_TARGET_CONFLICT": "这个记忆目标正由另一项写入占用，请稍后重新准备。",
        "APPROVAL_ALREADY_BOUND": "这份提案已绑定另一条确认记录，请重新准备。",
        "APPROVAL_REQUIRED": "缺少对当前提案的明确用户确认。",
        "APPLY_RECOVERY_REQUIRED": "目标已进入应用阶段且内容发生变化，必须重试应用与收尾，不能取消。",
        "BASE_NOT_AT_GIT_HEAD": "目标记忆已有尚未收尾的修改，不能覆盖。",
        "CLAIM_MISSING_FOR_RECOVERY": "上次写入的会话认领已丢失，需人工核对后再处理。",
        "CLOSEOUT_FAILED": "正式记忆收尾失败，修改仍保持认领状态，需核对后重试。",
        "CLOSEOUT_TIMEOUT": "正式记忆收尾超时，修改仍保持认领状态，需核对后重试。",
        "CONFIRMATION_INVALID": "用户确认记录格式无效。",
        "CONFIRMATION_REQUIRED": "必须明确确认这一个目标和这一版内容。",
        "CONTENT_NOT_UTF8": "目标记忆不是有效的 UTF-8 Markdown。",
        "CONTENT_TOO_LARGE": "提案内容超过 Agent Memory 的大小限制。",
        "INTENT_EXPIRED": "这份提案已过期，请重新准备并确认。",
        "INTENT_SESSION_MISMATCH": "这份提案不属于当前插件会话，请重新准备。",
        "MERGE_REQUIRED": "发现可能重复或冲突的记忆，需要先选择合并方式。",
        "NOOP": "正式记忆中已经有等价内容，不需要再次写入。",
        "PROPOSAL_CONTENT_MISMATCH": "确认的内容与准备阶段不是同一版。",
        "PROPOSAL_HASH_MISMATCH": "确认的内容哈希与准备阶段不一致。",
        "RECONCILE_UNAVAILABLE": "当前无法完成正式记忆查重，未创建写入提案。",
        "READ_TOKEN_REQUIRED": "必须先读取这个目标并提交同一版读取令牌。",
        "SCOPE_METADATA_INVALID": "目标或提案不属于当前创作记忆范围。",
        "SESSION_REQUIRED": "插件没有独立的 Agent Memory 会话，不能写入。",
        "SOURCE_METADATA_INVALID": "记忆来源信息不完整或不受支持。",
        "STALE_BASE": "准备后目标记忆发生了变化，请重新准备。",
        "STALE_READ_TOKEN": "读取目标后内容或 Git 基线已变化，请重新读取再准备。",
        "TARGET_ALREADY_EXISTS": "准备新建的目标已经存在，需要重新选择或改为更新。",
        "TARGET_CHANGED_AFTER_CLAIM": "认领后目标内容发生变化，已停止写入。",
        "TARGET_MISSING": "准备更新的目标已不存在，请重新准备。",
        "TARGET_PARENT_MISSING": "目标目录不存在，请先选择现有的正式记忆目录。",
        "TARGET_REQUIRED": "新建记忆前必须先选择具体保存文件。",
        "TARGET_RECOMMENDATION_CONFLICT": "选择的目标与查重结果不一致，需要人工确认合并。",
        "TARGET_TOO_LARGE": "目标记忆超过宿主可安全读取的大小上限。",
        "TARGET_UNREADABLE": "无法读取目标记忆。",
        "TARGET_WRITE_RECOVERY_REQUIRED": "检测到未完成或再次竞态的原子写入，已保留恢复材料并停止；需人工核对后重试。",
        "ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE": "当前文件系统不支持安全的原子条件写入，未修改目标记忆。",
        "USER_CONFIRMATION_REQUIRED": "只有用户本人明确确认后才能写入正式记忆。",
        "WRITE_LOCK_TIMEOUT": "另一项正式记忆写入仍在进行，请稍后重试。",
    }
    return messages.get(reason_code, "正式记忆写入协议已停止本次操作。")


def _raise(reason_code: str, *, retryable: bool = False) -> None:
    code = _safe_code(reason_code)
    raise StudioWriteError(code, _error_message(code), retryable=retryable)


def _raw_session_id() -> str:
    value = os.environ.get("AGENT_MEMORY_SESSION_ID", "").strip()
    if not value:
        _raise("SESSION_REQUIRED")
    return value


@contextlib.contextmanager
def studio_write_lock(timeout: float) -> Iterator[None]:
    try:
        ensure_private_directory(WRITE_LOCK_PATH.parent, harden_existing=True)
    except StateSecurityError as exc:
        raise StudioWriteError(
            "WRITE_LOCK_UNSAFE",
            "正式记忆写入锁目录不安全。",
        ) from exc
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(WRITE_LOCK_PATH, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise StudioWriteError("WRITE_LOCK_UNAVAILABLE", "无法打开正式记忆写入锁。") from exc
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        if POSIX_PERMISSION_MODEL:
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                if try_lock(handle):
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                _raise("WRITE_LOCK_TIMEOUT", retryable=True)
            time.sleep(0.1)
        try:
            yield
        finally:
            unlock(handle)


def _read_request() -> dict[str, Any]:
    max_request_bytes = max(write_intent.MAX_PROPOSAL_BYTES * 4 + 256 * 1024, 1024 * 1024)
    payload = sys.stdin.buffer.read(max_request_bytes + 1)
    if len(payload) > max_request_bytes:
        _raise("CONTENT_TOO_LARGE")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudioWriteError("REQUEST_INVALID", "Agent Memory 写入请求格式无效。") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        _raise("REQUEST_INVALID")
    return value


def _required_string(
    payload: dict[str, Any],
    key: str,
    *,
    max_chars: int,
    preserve: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        _raise("REQUEST_INVALID")
    result = value if preserve else value.strip()
    if not result or len(result) > max_chars or "\x00" in result:
        _raise("REQUEST_INVALID")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in result):
        _raise("REQUEST_INVALID")
    return result


def _optional_string(payload: dict[str, Any], key: str, *, max_chars: int) -> str:
    value = payload.get(key, "")
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        _raise("REQUEST_INVALID")
    result = value.strip()
    if len(result) > max_chars or "\x00" in result:
        _raise("REQUEST_INVALID")
    if any(ord(character) < 32 for character in result):
        _raise("REQUEST_INVALID")
    return result


def _proposal_text(payload: dict[str, Any]) -> tuple[str, write_intent.ContentDigest]:
    value = payload.get("proposal_markdown")
    if not isinstance(value, str) or not value or "\x00" in value:
        _raise("REQUEST_INVALID")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        _raise("REQUEST_INVALID")
    try:
        digest = write_intent.content_hashes(
            value.encode("utf-8"),
            max_bytes=write_intent.MAX_PROPOSAL_BYTES,
        )
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    return value, digest


def _scope_request(payload: dict[str, Any]) -> tuple[str, str]:
    raw_app_id = payload.get("app_id")
    if not isinstance(raw_app_id, str) or not raw_app_id.strip():
        raise StudioWriteError("APP_ID_REQUIRED", _error_message("SCOPE_METADATA_INVALID"))
    app_id = raw_app_id.strip()
    if len(app_id) > 160 or "\x00" in app_id:
        _raise("SCOPE_METADATA_INVALID")
    project_id = _optional_string(payload, "project_id", max_chars=160)
    try:
        return memory_retrieve.validate_studio_scope_request(app_id, project_id)
    except memory_retrieve.RetrievalProtocolError as exc:
        raise StudioWriteError(
            exc.code,
            _error_message("SCOPE_METADATA_INVALID"),
        ) from exc


def _validate_studio_markdown(
    text: str,
    *,
    path: Path,
    app_id: str,
    project_id: str,
    require_explicit_write_scope: bool,
) -> dict[str, Any]:
    if memory_retrieve._contains_secret(text):
        _raise("SECRET_MATERIAL")
    try:
        parsed = memory_retrieve._frontmatter_text(text)
        metadata = memory_retrieve._metadata_for(path, parsed)
    except (memory_retrieve.RetrievalProtocolError, OSError, ValueError) as exc:
        reason = exc.code if isinstance(exc, memory_retrieve.RetrievalProtocolError) else "FRONTMATTER_INVALID"
        raise StudioWriteError(reason, _error_message("SCOPE_METADATA_INVALID")) from exc
    rejection = memory_retrieve._metadata_rejection(metadata, ACTOR, app_id, project_id)
    if rejection:
        raise StudioWriteError(rejection, _error_message("SCOPE_METADATA_INVALID"))
    if require_explicit_write_scope:
        meta = metadata.get("meta")
        if not isinstance(meta, dict):
            _raise("SCOPE_METADATA_INVALID")
        for key in ("status", "agent_scope", "app_id"):
            if key not in meta:
                _raise("SCOPE_METADATA_INVALID")
        if str(meta.get("status", "")).strip().casefold() != "active":
            _raise("STATUS_NOT_ACTIVE")
        if str(meta.get("agent_scope", "")).strip().casefold() != "shared":
            _raise("AGENT_SCOPE_MISMATCH")
        if memory_retrieve._normalized_values(str(meta.get("app_id", ""))) != {app_id}:
            _raise("APP_ID_MISMATCH")
        proposal_projects = memory_retrieve._normalized_values(str(meta.get("project_id", "")))
        if project_id:
            if proposal_projects != {project_id}:
                _raise("PROJECT_SCOPE_MISMATCH")
        elif proposal_projects and not proposal_projects <= {"global", "shared"}:
            _raise("PROJECT_SCOPE_MISMATCH")
    return metadata


def _read_token(
    target: write_intent.CanonicalTarget,
    *,
    app_id: str,
    project_id: str,
    exists: bool,
    digest: write_intent.ContentDigest,
    git_head: str,
    raw_session_id: str,
) -> str:
    material = json.dumps(
        {
            "schema": "studio-read-token-v1",
            "target_key": target.target_key,
            "relative_path": target.rel_path,
            "app_id": app_id,
            "project_id": project_id,
            "base_exists": bool(exists),
            "base_raw_sha256": digest.raw_sha256,
            "base_canonical_sha256": digest.canonical_sha256,
            "base_git_head": git_head,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(raw_session_id.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _target_snapshot(
    target: write_intent.CanonicalTarget,
    *,
    app_id: str,
    project_id: str,
    host_read: bool,
    raw_session_id: str,
) -> tuple[bool, write_intent.ContentDigest, str, str]:
    exists, digest = _read_host_target(target) if host_read else _target_digest(target)
    if exists:
        _validate_studio_markdown(
            digest.text,
            path=target.path,
            app_id=app_id,
            project_id=project_id,
            require_explicit_write_scope=False,
        )
    try:
        git_head = write_intent.current_git_head(required=True)
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    return exists, digest, git_head, _read_token(
        target,
        app_id=app_id,
        project_id=project_id,
        exists=exists,
        digest=digest,
        git_head=git_head,
        raw_session_id=raw_session_id,
    )


def _formal_target(raw_target: str) -> write_intent.CanonicalTarget:
    try:
        target = write_intent.canonical_target(raw_target)
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    relative = Path(target.rel_path)
    if len(relative.parts) < 2 or relative.parts[0] not in FORMAL_MEMORY_TOP_LEVELS:
        raise StudioWriteError(
            "TARGET_NOT_FORMAL_MEMORY",
            "目标必须位于正式 Agent Memory 目录。",
        )
    if not target.path.parent.is_dir():
        _raise("TARGET_PARENT_MISSING")
    return target


def _target_digest(target: write_intent.CanonicalTarget) -> tuple[bool, write_intent.ContentDigest]:
    try:
        exists, digest = write_intent._read_target(target)  # Shared canonical read boundary.
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    return exists, digest


def _read_host_target(
    target: write_intent.CanonicalTarget,
) -> tuple[bool, write_intent.ContentDigest]:
    """Read one formal target exactly, without touching runtime state or logs."""
    if not target.path.exists():
        return False, write_intent.content_hashes(b"")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(target.path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _raise("TARGET_NOT_FILE")
        if metadata.st_size > MAX_HOST_TARGET_BYTES:
            _raise("TARGET_TOO_LARGE")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(MAX_HOST_TARGET_BYTES + 1)
    except StudioWriteError:
        raise
    except OSError as exc:
        raise StudioWriteError(
            "TARGET_UNREADABLE",
            _error_message("TARGET_UNREADABLE"),
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > MAX_HOST_TARGET_BYTES:
        _raise("TARGET_TOO_LARGE")
    try:
        return True, write_intent.content_hashes(payload, max_bytes=MAX_HOST_TARGET_BYTES)
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)


def read_target(request: dict[str, Any], *, raw_session_id: str) -> dict[str, Any]:
    app_id, project_id = _scope_request(request)
    raw_target = _required_string(request, "target_relative_path", max_chars=512)
    target = _formal_target(raw_target)
    exists, digest, git_head, read_token = _target_snapshot(
        target,
        app_id=app_id,
        project_id=project_id,
        host_read=True,
        raw_session_id=raw_session_id,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "stage": "read-target",
        "status": "found" if exists else "missing",
        "target_relative_path": target.rel_path,
        "exists": exists,
        "base_exists": exists,
        "content": digest.text,
        "raw_sha256": digest.raw_sha256,
        "canonical_sha256": digest.canonical_sha256,
        "base_raw_sha256": digest.raw_sha256,
        "base_canonical_sha256": digest.canonical_sha256,
        "size_bytes": digest.size_bytes,
        "git_head": git_head,
        "base_git_head": git_head,
        "read_token": read_token,
        "app_id": app_id,
        "project_id": project_id,
    }


def _safe_candidate(
    row: dict[str, Any],
    *,
    app_id: str,
    project_id: str,
    raw_session_id: str,
) -> dict[str, Any] | None:
    raw_path = str(row.get("rel_path", "")).strip()
    if not raw_path:
        return None
    try:
        target = _formal_target(raw_path)
        exists, digest, _, _ = _target_snapshot(
            target,
            app_id=app_id,
            project_id=project_id,
            host_read=False,
            raw_session_id=raw_session_id,
        )
    except StudioWriteError:
        return None
    if not exists:
        return None
    material = f"{target.rel_path}\0{digest.raw_sha256}".encode("utf-8")
    return {
        "relative_path": target.rel_path,
        "sha256": digest.raw_sha256,
        "candidate_ref": hashlib.sha256(material).hexdigest()[:20],
    }


def _warning_codes(warnings: list[str]) -> list[str]:
    codes: set[str] = set()
    for warning in warnings:
        lowered = warning.casefold()
        if "missing" in lowered:
            codes.add("SEARCH_INDEX_MISSING")
        elif "timeout" in lowered or "timed out" in lowered:
            codes.add("SEARCH_TIMEOUT")
        elif "failed" in lowered or "non-json" in lowered:
            codes.add("SEARCH_BACKEND_FAILED")
        else:
            codes.add("SEARCH_DEGRADED")
    return sorted(codes)


def _record_prepare_safety(
    assessment: dict[str, Any],
    *,
    raw_session_id: str,
) -> None:
    try:
        memory_safety.record_assessment(
            write_intent.STATE_DB,
            assessment,
            run_id=f"studio-prepare:{uuid.uuid4().hex}",
            actor=ACTOR,
            session_hash=write_intent.session_hash(raw_session_id),
            trigger="studio_write_prepare",
        )
    except (OSError, ValueError) as exc:
        raise StudioWriteError(
            "SAFETY_AUDIT_UNAVAILABLE",
            "无法记录来源安全审计，未创建写入提案。",
        ) from exc


def _base_response(
    *,
    status: str,
    action: str,
    digest: write_intent.ContentDigest,
    target: str = "",
    candidates: list[dict[str, Any]] | None = None,
    warning_codes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "stage": "prepare",
        "status": status,
        "recommended_action": action,
        "target_relative_path": target,
        "proposal_raw_sha256": digest.raw_sha256,
        "proposal_canonical_sha256": digest.canonical_sha256,
        "proposal_size_bytes": digest.size_bytes,
        "confirmation_required": action in WRITABLE_ACTIONS,
        "candidates": candidates or [],
        "warnings": warning_codes or [],
    }


def prepare(request: dict[str, Any], *, raw_session_id: str) -> dict[str, Any]:
    proposal, digest = _proposal_text(request)
    app_id, project_id = _scope_request(request)
    target_request = _required_string(request, "target_relative_path", max_chars=512)
    selected_target = _formal_target(target_request)
    raw_read_token = request.get("read_token")
    read_token = raw_read_token.strip() if isinstance(raw_read_token, str) else ""
    if READ_TOKEN_RE.fullmatch(read_token) is None:
        _raise("READ_TOKEN_REQUIRED")
    _validate_studio_markdown(
        proposal,
        path=selected_target.path,
        app_id=app_id,
        project_id=project_id,
        require_explicit_write_scope=True,
    )
    target_exists, target_digest, target_git_head, current_read_token = _target_snapshot(
        selected_target,
        app_id=app_id,
        project_id=project_id,
        host_read=False,
        raw_session_id=raw_session_id,
    )
    if current_read_token != read_token:
        _raise("STALE_READ_TOKEN", retryable=True)
    summary = _required_string(request, "summary", max_chars=MAX_SUMMARY_CHARS)
    source_class = _required_string(request, "source_class", max_chars=80)
    knowledge_kind = _required_string(request, "knowledge_kind", max_chars=80)
    asserted_by = _required_string(request, "asserted_by", max_chars=80)
    evidence_ref = _optional_string(request, "evidence_ref", max_chars=MAX_REFERENCE_CHARS)
    legacy_project = _optional_string(request, "current_project", max_chars=160)
    if legacy_project and legacy_project.casefold() != (project_id or ACTOR).casefold():
        _raise("PROJECT_SCOPE_MISMATCH")
    if asserted_by not in ASSERTED_BY_VALUES:
        _raise("SOURCE_METADATA_INVALID")
    try:
        assessment = memory_safety.assess_source(
            f"{summary}\n{proposal}",
            source_class=source_class,
            knowledge_kind=knowledge_kind,
            asserted_by=asserted_by,
            evidence_ref=evidence_ref,
        )
    except ValueError as exc:
        raise StudioWriteError(
            "SOURCE_METADATA_INVALID",
            _error_message("SOURCE_METADATA_INVALID"),
        ) from exc
    _record_prepare_safety(assessment, raw_session_id=raw_session_id)
    if str(assessment.get("decision")) != "ALLOW":
        reason_code = _safe_code(str(assessment.get("reason_code", "SOURCE_REJECTED")))
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "stage": "prepare",
            "status": "blocked",
            "reason_code": reason_code,
            "message": _error_message(reason_code),
            "retryable": False,
            "proposal_raw_sha256": digest.raw_sha256,
            "proposal_canonical_sha256": digest.canonical_sha256,
        }

    rows, warnings, backend_status = memory_closeout.search_memory(
        summary,
        limit=8,
        no_zvec=not memory_closeout.SEMANTIC_ENABLED,
        current_project=project_id,
        read_only=True,
        app_id=app_id,
        agent_scope="shared",
        project_id=project_id,
    )
    if backend_status.get("sqlite", {}).get("status") != "ok":
        _raise("RECONCILE_UNAVAILABLE", retryable=True)
    scoped_rows: list[dict[str, Any]] = []
    scoped_candidates: list[dict[str, Any]] = []
    for row in rows:
        candidate = _safe_candidate(
            row,
            app_id=app_id,
            project_id=project_id,
            raw_session_id=raw_session_id,
        )
        if candidate is None:
            continue
        scoped_rows.append(row)
        if len(scoped_candidates) < 5:
            scoped_candidates.append(candidate)
    rows = scoped_rows
    action, recommended_row, metrics = memory_closeout.prewrite_recommendation(summary, rows)
    if action not in {"ADD", "UPDATE", "NOOP", "MERGE_REQUIRED"}:
        action = "MERGE_REQUIRED"
    candidates = scoped_candidates
    warning_codes = _warning_codes(warnings)
    recommended_path = ""
    if isinstance(recommended_row, dict):
        candidate = _safe_candidate(
            recommended_row,
            app_id=app_id,
            project_id=project_id,
            raw_session_id=raw_session_id,
        )
        if candidate is not None:
            recommended_path = str(candidate["relative_path"])

    if recommended_path and action in {"UPDATE", "NOOP"}:
        selected = selected_target
        recommended = _formal_target(recommended_path)
        if selected.target_key != recommended.target_key:
            response = _base_response(
                status="merge_required",
                action="MERGE_REQUIRED",
                digest=digest,
                target=recommended.rel_path,
                candidates=candidates,
                warning_codes=warning_codes,
            )
            response["reason_code"] = "TARGET_RECOMMENDATION_CONFLICT"
            return response

    if action == "NOOP":
        return _base_response(
            status="noop",
            action="NOOP",
            digest=digest,
            target=recommended_path,
            candidates=candidates,
            warning_codes=warning_codes,
        )
    if action == "MERGE_REQUIRED":
        response = _base_response(
            status="merge_required",
            action="MERGE_REQUIRED",
            digest=digest,
            target=recommended_path,
            candidates=candidates,
            warning_codes=warning_codes,
        )
        response["reason_code"] = "MERGE_REQUIRED"
        return response

    if action != "ADD":
        if not recommended_path:
            response = _base_response(
                status="merge_required",
                action="MERGE_REQUIRED",
                digest=digest,
                candidates=candidates,
                warning_codes=warning_codes,
            )
            response["reason_code"] = "MERGE_REQUIRED"
            return response
        recommended_target = _formal_target(recommended_path)
        if recommended_target.target_key != selected_target.target_key:
            response = _base_response(
                status="merge_required",
                action="MERGE_REQUIRED",
                digest=digest,
                target=recommended_target.rel_path,
                candidates=candidates,
                warning_codes=warning_codes,
            )
            response["reason_code"] = "TARGET_RECOMMENDATION_CONFLICT"
            return response
    if action == "ADD" and target_exists:
        response = _base_response(
            status="merge_required",
            action="MERGE_REQUIRED",
            digest=digest,
            target=selected_target.rel_path,
            candidates=candidates,
            warning_codes=warning_codes,
        )
        response["reason_code"] = "TARGET_ALREADY_EXISTS"
        return response
    if action == "UPDATE" and not target_exists:
        _raise("TARGET_MISSING")
    if target_exists and target_digest.canonical_sha256 == digest.canonical_sha256:
        return _base_response(
            status="noop",
            action="NOOP",
            digest=digest,
            target=selected_target.rel_path,
            candidates=candidates,
            warning_codes=warning_codes,
        )

    ttl_raw = request.get("ttl_hours", 1)
    if not isinstance(ttl_raw, (int, float)) or isinstance(ttl_raw, bool):
        _raise("REQUEST_INVALID")
    ttl_hours = min(max(float(ttl_raw), 0.25), 24.0)
    try:
        intent = write_intent.create_intent(
            actor=ACTOR,
            raw_session_id=raw_session_id,
            target=selected_target.path,
            proposal_text=proposal,
            approval_required=True,
            ttl_hours=ttl_hours,
            source_class=source_class,
            knowledge_kind=knowledge_kind,
            asserted_by=asserted_by,
            evidence_ref_sha256=str(assessment.get("evidence_ref_sha256", "")),
            reconcile_action=action,
            strict_git_base=True,
            store_proposal_snapshot=False,
            read_token=read_token,
            scope_app_id=app_id,
            scope_project_id=project_id,
            expected_base_exists=target_exists,
            expected_base_raw_sha256=target_digest.raw_sha256,
            expected_base_canonical_sha256=target_digest.canonical_sha256,
            expected_base_git_head=target_git_head,
        )
    except write_intent.IntentError as exc:
        _raise(exc.reason_code, retryable=exc.reason_code == "ACTIVE_TARGET_CONFLICT")

    response = _base_response(
        status="prepared",
        action=action,
        digest=digest,
        target=selected_target.rel_path,
        candidates=candidates,
        warning_codes=warning_codes,
    )
    response.update(
        {
            "proposal_id": str(intent["intent_id"]),
            "base_exists": bool(intent["base_exists"]),
            "base_raw_sha256": str(intent["base_raw_sha256"]),
            "base_canonical_sha256": str(intent["base_canonical_sha256"]),
            "base_git_head": str(intent["base_git_head"]),
            "expires_at": str(intent["expires_at"]),
            "recommendation_metrics": {
                "similarity": round(float(metrics.get("similarity") or 0), 4),
                "coverage": round(float(metrics.get("coverage") or 0), 4),
                "raw_semantic_distance": (
                    round(float(metrics["raw_semantic_distance"]), 4)
                    if metrics.get("raw_semantic_distance") is not None
                    else None
                ),
            },
        }
    )
    return response


def _authorized_intent(proposal_id: str, *, raw_session_id: str) -> dict[str, Any]:
    try:
        shown = write_intent.show_intent(proposal_id)
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    intent = shown.get("intent")
    if not isinstance(intent, dict):
        _raise("INTENT_NOT_FOUND")
    if (
        str(intent.get("actor", "")) != ACTOR
        or str(intent.get("session_hash", "")) != write_intent.session_hash(raw_session_id)
    ):
        _raise("INTENT_SESSION_MISMATCH")
    return intent


def _intent_scope_binding(
    intent: dict[str, Any],
    *,
    target: write_intent.CanonicalTarget,
    raw_session_id: str,
) -> tuple[str, str]:
    app_id = str(intent.get("scope_app_id", ""))
    project_id = str(intent.get("scope_project_id", ""))
    try:
        app_id, project_id = memory_retrieve.validate_studio_scope_request(app_id, project_id)
    except memory_retrieve.RetrievalProtocolError as exc:
        raise StudioWriteError("INTENT_SCOPE_INVALID", _error_message("SCOPE_METADATA_INVALID")) from exc
    stored_token = str(intent.get("read_token", ""))
    if READ_TOKEN_RE.fullmatch(stored_token) is None:
        _raise("READ_TOKEN_REQUIRED")
    base_digest = write_intent.ContentDigest(
        raw_sha256=str(intent.get("base_raw_sha256", "")),
        canonical_sha256=str(intent.get("base_canonical_sha256", "")),
        size_bytes=0,
        text="",
    )
    expected = _read_token(
        target,
        app_id=app_id,
        project_id=project_id,
        exists=bool(intent.get("base_exists", 0)),
        digest=base_digest,
        git_head=str(intent.get("base_git_head", "")),
        raw_session_id=raw_session_id,
    )
    if not hmac.compare_digest(stored_token, expected):
        _raise("INTENT_BINDING_INVALID")
    return app_id, project_id


def _validate_apply_request(
    request: dict[str, Any],
) -> tuple[str, str, write_intent.ContentDigest, str, str, str]:
    proposal_id = _required_string(request, "proposal_id", max_chars=64)
    if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        _raise("REQUEST_INVALID")
    target = _required_string(request, "target_relative_path", max_chars=512)
    proposal, digest = _proposal_text(request)
    raw_hash = _required_string(request, "proposal_raw_sha256", max_chars=64)
    canonical_hash = _required_string(request, "proposal_canonical_sha256", max_chars=64)
    if SHA256_RE.fullmatch(raw_hash) is None or SHA256_RE.fullmatch(canonical_hash) is None:
        _raise("REQUEST_INVALID")
    if digest.raw_sha256 != raw_hash or digest.canonical_sha256 != canonical_hash:
        _raise("PROPOSAL_HASH_MISMATCH")
    confirmed_by = _required_string(request, "confirmed_by", max_chars=40)
    if confirmed_by != "user":
        _raise("USER_CONFIRMATION_REQUIRED")
    confirmation_ref = _required_string(
        request,
        "confirmation_reference",
        max_chars=MAX_REFERENCE_CHARS,
    )
    if SAFE_REFERENCE_RE.fullmatch(confirmation_ref) is None:
        _raise("CONFIRMATION_INVALID")
    return proposal_id, target, digest, proposal, confirmed_by, confirmation_ref


def _approval_matches(intent: dict[str, Any], confirmation_ref: str) -> bool:
    return (
        str(intent.get("approved_by", "")) == "user"
        and str(intent.get("approval_ref_sha256", ""))
        == hashlib.sha256(confirmation_ref.encode("utf-8")).hexdigest()
        and str(intent.get("approval_proposal_raw_sha256", ""))
        == str(intent.get("proposal_raw_sha256", ""))
        and str(intent.get("approval_proposal_canonical_sha256", ""))
        == str(intent.get("proposal_canonical_sha256", ""))
    )


def _claim_matches(proposal_id: str, *, raw_session_id: str, target: Path) -> bool:
    try:
        rows = memory_claim.active_claim_rows(raw_session_id, ACTOR)
    except (OSError, ValueError):
        return False
    return any(
        str(row.get("intent_id", "")) == proposal_id
        and Path(str(row.get("path", ""))).resolve(strict=False) == target.resolve(strict=False)
        for row in rows
    )


def _conditional_sidecar_paths(
    target: write_intent.CanonicalTarget,
    proposal_id: str,
) -> tuple[Path, Path, Path]:
    if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        _raise("REQUEST_INVALID")
    token = hashlib.sha256(
        f"studio-cas:{proposal_id}:{target.target_key}".encode("utf-8")
    ).hexdigest()[:24]
    prefix = target.path.parent / f".agent-memory-studio-{token}"
    return (
        Path(f"{prefix}.proposal"),
        Path(f"{prefix}.displaced"),
        Path(f"{prefix}.recovery"),
    )


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_payload(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)
        return payload
    except StudioWriteError:
        raise
    except OSError as exc:
        raise StudioWriteError(
            "TARGET_WRITE_RECOVERY_REQUIRED",
            _error_message("TARGET_WRITE_RECOVERY_REQUIRED"),
            retryable=True,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_exact_sidecar(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            if POSIX_PERMISSION_MODEL:
                os.fchmod(handle.fileno(), mode)
    except FileExistsError as exc:
        raise StudioWriteError(
            "TARGET_WRITE_RECOVERY_REQUIRED",
            _error_message("TARGET_WRITE_RECOVERY_REQUIRED"),
            retryable=True,
        ) from exc
    except OSError as exc:
        raise StudioWriteError("TARGET_WRITE_FAILED", "无法准备原子写入文件。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_known_sidecar(path: Path, expected_sha256: str) -> None:
    if not _path_present(path):
        return
    payload = _read_regular_payload(
        path,
        max_bytes=max(
            MAX_HOST_TARGET_BYTES,
            write_intent.MAX_TARGET_BYTES,
            write_intent.MAX_PROPOSAL_BYTES,
        ),
    )
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)
    try:
        path.unlink()
    except OSError as exc:
        raise StudioWriteError(
            "TARGET_WRITE_RECOVERY_REQUIRED",
            _error_message("TARGET_WRITE_RECOVERY_REQUIRED"),
            retryable=True,
        ) from exc


def _atomic_exchange(first: Path, second: Path) -> None:
    """Atomically exchange two same-filesystem paths or fail without replacing either."""

    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        rename_swap = getattr(libc, "renamex_np", None)
        if rename_swap is None:
            _raise("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE")
        rename_swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_swap.restype = ctypes.c_int
        result = rename_swap(os.fsencode(first), os.fsencode(second), 0x00000002)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename_swap = getattr(libc, "renameat2", None)
        if rename_swap is None:
            _raise("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE")
        rename_swap.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_swap.restype = ctypes.c_int
        result = rename_swap(-100, os.fsencode(first), -100, os.fsencode(second), 0x00000002)
    else:
        _raise("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        _raise("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE")
    raise OSError(error_number, os.strerror(error_number))


def _windows_replace_with_backup(target: Path, replacement: Path, backup: Path) -> None:
    if os.name != "nt":
        _raise("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    if replace_file(str(target), str(replacement), str(backup), 0x00000001, None, None):
        return
    error_number = ctypes.get_last_error()
    raise OSError(error_number, os.strerror(error_number))


def _atomic_capture_target(
    proposal_path: Path,
    target_path: Path,
    displaced_path: Path,
) -> Path:
    if os.name == "nt":
        _windows_replace_with_backup(target_path, proposal_path, displaced_path)
        return displaced_path
    _atomic_exchange(proposal_path, target_path)
    return proposal_path


def _atomic_restore_target(
    captured_path: Path,
    target_path: Path,
    proposal_path: Path,
) -> Path:
    if os.name == "nt":
        _windows_replace_with_backup(target_path, captured_path, proposal_path)
        return proposal_path
    _atomic_exchange(captured_path, target_path)
    return captured_path


def _atomic_conditional_write(
    target: write_intent.CanonicalTarget,
    proposal: str,
    *,
    proposal_id: str,
    expected_exists: bool,
    expected_raw_sha256: str,
) -> None:
    """Publish exact bytes without ever discarding a raced target version.

    ADD uses an atomic no-replace hard link.  UPDATE atomically exchanges the
    proposal with the target, then validates the displaced bytes.  A mismatch
    is exchanged back; an independent recovery copy protects the displaced
    bytes from any second race during restoration.  Uncertain recovery leaves
    the sidecars in place and fails closed.
    """

    if not target.path.parent.is_dir():
        _raise("TARGET_PARENT_MISSING")
    proposal_path, displaced_path, recovery_path = _conditional_sidecar_paths(
        target,
        proposal_id,
    )
    if any(_path_present(path) for path in (proposal_path, displaced_path, recovery_path)):
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)

    current_mode = 0o644
    if expected_exists:
        try:
            metadata = target.path.lstat()
        except OSError as exc:
            raise StudioWriteError(
                "TARGET_CHANGED_AFTER_CLAIM",
                _error_message("TARGET_CHANGED_AFTER_CLAIM"),
                retryable=True,
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            _raise("TARGET_CHANGED_AFTER_CLAIM", retryable=True)
        current_mode = stat.S_IMODE(metadata.st_mode) or 0o644

    proposal_payload = proposal.encode("utf-8")
    proposal_sha256 = hashlib.sha256(proposal_payload).hexdigest()
    _create_exact_sidecar(proposal_path, proposal_payload, current_mode)
    _fsync_directory(target.path.parent)

    if not expected_exists:
        try:
            os.link(proposal_path, target.path)
            _fsync_directory(target.path.parent)
        except FileExistsError as exc:
            _unlink_known_sidecar(proposal_path, proposal_sha256)
            raise StudioWriteError(
                "TARGET_CHANGED_AFTER_CLAIM",
                _error_message("TARGET_CHANGED_AFTER_CLAIM"),
                retryable=True,
            ) from exc
        except OSError as exc:
            _unlink_known_sidecar(proposal_path, proposal_sha256)
            raise StudioWriteError(
                "ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE",
                _error_message("ATOMIC_CONDITIONAL_WRITE_UNAVAILABLE"),
            ) from exc
        _unlink_known_sidecar(proposal_path, proposal_sha256)
        _fsync_directory(target.path.parent)
        return

    try:
        captured_path = _atomic_capture_target(proposal_path, target.path, displaced_path)
    except StudioWriteError:
        if _path_present(proposal_path):
            _unlink_known_sidecar(proposal_path, proposal_sha256)
        raise
    except OSError as exc:
        if _path_present(proposal_path):
            _unlink_known_sidecar(proposal_path, proposal_sha256)
        raise StudioWriteError(
            "TARGET_CHANGED_AFTER_CLAIM",
            _error_message("TARGET_CHANGED_AFTER_CLAIM"),
            retryable=True,
        ) from exc
    _fsync_directory(target.path.parent)

    max_capture_bytes = max(MAX_HOST_TARGET_BYTES, write_intent.MAX_TARGET_BYTES)
    try:
        captured_payload = _read_regular_payload(captured_path, max_bytes=max_capture_bytes)
    except StudioWriteError:
        # The exchanged object itself is still preserved at captured_path.  Try
        # to put it back, but retain the returned proposal sidecar because the
        # object could not be bounded and verified.
        try:
            _atomic_restore_target(captured_path, target.path, proposal_path)
            _fsync_directory(target.path.parent)
        except (OSError, StudioWriteError):
            pass
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)

    captured_sha256 = hashlib.sha256(captured_payload).hexdigest()
    if captured_sha256 == expected_raw_sha256:
        _unlink_known_sidecar(captured_path, expected_raw_sha256)
        _fsync_directory(target.path.parent)
        return

    # Preserve the exact raced bytes independently before attempting the
    # rollback.  If a second writer changes the target during that rollback,
    # both the first displaced version and the newly displaced version remain
    # recoverable instead of being silently deleted.
    _create_exact_sidecar(recovery_path, captured_payload, PRIVATE_FILE_MODE)
    _fsync_directory(target.path.parent)
    try:
        returned_proposal_path = _atomic_restore_target(
            captured_path,
            target.path,
            proposal_path,
        )
        _fsync_directory(target.path.parent)
    except (OSError, StudioWriteError) as exc:
        raise StudioWriteError(
            "TARGET_WRITE_RECOVERY_REQUIRED",
            _error_message("TARGET_WRITE_RECOVERY_REQUIRED"),
            retryable=True,
        ) from exc

    try:
        restored_payload = _read_regular_payload(target.path, max_bytes=max_capture_bytes)
        returned_payload = _read_regular_payload(
            returned_proposal_path,
            max_bytes=max(MAX_HOST_TARGET_BYTES, write_intent.MAX_PROPOSAL_BYTES),
        )
    except StudioWriteError:
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)
    restored_sha256 = hashlib.sha256(restored_payload).hexdigest()
    returned_sha256 = hashlib.sha256(returned_payload).hexdigest()
    if restored_sha256 != captured_sha256 or returned_sha256 != proposal_sha256:
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)

    _unlink_known_sidecar(returned_proposal_path, proposal_sha256)
    _unlink_known_sidecar(recovery_path, captured_sha256)
    _fsync_directory(target.path.parent)
    _raise("TARGET_CHANGED_AFTER_CLAIM", retryable=True)


def _has_conditional_recovery_sidecar(
    target: write_intent.CanonicalTarget,
    proposal_id: str,
) -> bool:
    return any(
        _path_present(path)
        for path in _conditional_sidecar_paths(target, proposal_id)
    )


def _terminalize_safe_concurrent_failure(
    proposal_id: str,
    *,
    raw_session_id: str,
    target: Path,
) -> None:
    """Close an intent only after the concurrent target bytes are back in place."""

    try:
        write_intent.finalize_receipt(
            proposal_id,
            actor=ACTOR,
            raw_session_id=raw_session_id,
            outcome="failed",
            reason_code="TARGET_CHANGED_AFTER_CLAIM",
            detail_code="ATOMIC_CAS_REJECTED",
        )
        memory_claim.complete_claim_paths(raw_session_id, ACTOR, [target])
    except (OSError, sqlite3.Error, ValueError, write_intent.IntentError) as exc:
        raise StudioWriteError(
            "APPLY_RECOVERY_REQUIRED",
            _error_message("APPLY_RECOVERY_REQUIRED"),
            retryable=True,
        ) from exc


def _process_group_exists(group_id: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process: subprocess.Popen[str], group_id: int, sig: int) -> None:
    if os.name == "nt":
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass


def _shutdown_process_group(
    process: subprocess.Popen[str],
    group_id: int,
    *,
    term_grace: float = 1.0,
    kill_grace: float = 5.0,
) -> None:
    """Terminate, reap, and verify a transport tree before its caller unlocks."""

    _signal_process_group(process, group_id, signal.SIGTERM)
    try:
        process.communicate(timeout=term_grace)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, group_id, signal.SIGKILL)
        try:
            process.communicate(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ValueError):
        pass
    if process.poll() is None:
        _signal_process_group(process, group_id, signal.SIGKILL)
        try:
            process.wait(timeout=kill_grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if os.name != "nt":
        if _process_group_exists(group_id):
            _signal_process_group(process, group_id, signal.SIGKILL)
        deadline = time.monotonic() + kill_grace
        while _process_group_exists(group_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _process_group_exists(group_id):
            raise StudioWriteError(
                "CLOSEOUT_SHUTDOWN_FAILED",
                "正式记忆收尾进程组未能完全停止，仍保持写入锁到安全停止边界。",
                retryable=True,
            )


def _run_closeout(
    *,
    raw_session_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    environment = os.environ.copy()
    for key in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        environment.pop(key, None)
    environment["AGENT_MEMORY_SESSION_ID"] = raw_session_id
    environment["MEMORY_ACTOR"] = ACTOR
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")
    command = [
        PYTHON,
        str(CLOSEOUT_SCRIPT),
        "--actor",
        ACTOR,
        "--claimed-only",
        "--commit",
        "--json",
        "--trigger",
        "manual",
        "--skip-audit",
        "--lock-timeout",
        "30",
    ]
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_options,
        )
    except OSError as exc:
        raise StudioWriteError("CLOSEOUT_FAILED", _error_message("CLOSEOUT_FAILED")) from exc
    group_id = process.pid
    try:
        stdout, stderr = process.communicate(timeout=max(timeout_seconds, 1))
    except subprocess.TimeoutExpired as exc:
        _shutdown_process_group(process, group_id)
        raise StudioWriteError(
            "CLOSEOUT_TIMEOUT",
            _error_message("CLOSEOUT_TIMEOUT"),
            retryable=True,
        ) from exc
    except BaseException:
        _shutdown_process_group(process, group_id)
        raise
    if _process_group_exists(group_id):
        _shutdown_process_group(process, group_id)
        _raise("CLOSEOUT_FAILED", retryable=True)
    if len(stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _raise("CLOSEOUT_FAILED")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise StudioWriteError("CLOSEOUT_FAILED", _error_message("CLOSEOUT_FAILED")) from exc
    if not isinstance(payload, dict):
        _raise("CLOSEOUT_FAILED")
    if payload.get("reconcile_findings"):
        _raise("MERGE_REQUIRED")
    if process.returncode != 0 or payload.get("status") != "ok":
        _raise("CLOSEOUT_FAILED", retryable=True)
    return payload


def _completed_response(
    *,
    intent: dict[str, Any],
    receipt: dict[str, Any],
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "stage": "apply",
        "status": "applied",
        "proposal_id": str(intent.get("intent_id", "")),
        "recommended_action": str(intent.get("reconcile_action", "")),
        "target_relative_path": str(intent.get("target_rel_path", "")),
        "proposal_raw_sha256": str(intent.get("proposal_raw_sha256", "")),
        "proposal_canonical_sha256": str(intent.get("proposal_canonical_sha256", "")),
        "receipt_id": str(receipt.get("receipt_id", "")),
        "git_commit": str(receipt.get("git_commit", "")),
        "idempotent": idempotent,
        "completed_at": str(receipt.get("created_at", "")),
    }


def apply(
    request: dict[str, Any],
    *,
    raw_session_id: str,
    closeout_timeout: float,
) -> dict[str, Any]:
    proposal_id, raw_target, digest, proposal, _, confirmation_ref = _validate_apply_request(request)
    intent = _authorized_intent(proposal_id, raw_session_id=raw_session_id)
    action = str(intent.get("reconcile_action", "")).upper()
    if action not in WRITABLE_ACTIONS:
        _raise("MERGE_REQUIRED" if action == "MERGE_REQUIRED" else "NOOP")
    target = _formal_target(raw_target)
    if target.target_key != str(intent.get("target_key", "")):
        _raise("APPROVAL_TARGET_MISMATCH")
    if (
        digest.raw_sha256 != str(intent.get("proposal_raw_sha256", ""))
        or digest.canonical_sha256 != str(intent.get("proposal_canonical_sha256", ""))
    ):
        _raise("PROPOSAL_CONTENT_MISMATCH")
    app_id, project_id = _intent_scope_binding(
        intent,
        target=target,
        raw_session_id=raw_session_id,
    )
    _validate_studio_markdown(
        proposal,
        path=target.path,
        app_id=app_id,
        project_id=project_id,
        require_explicit_write_scope=True,
    )
    if _has_conditional_recovery_sidecar(target, proposal_id):
        _raise("TARGET_WRITE_RECOVERY_REQUIRED", retryable=True)

    status = str(intent.get("status", ""))
    shown = write_intent.show_intent(proposal_id)
    receipt = shown.get("receipt")
    if status == "completed":
        if not isinstance(receipt, dict) or str(receipt.get("outcome", "")) != "completed":
            _raise("RECEIPT_OUTCOME_CONFLICT")
        exists, current = _target_digest(target)
        if not exists or current.raw_sha256 != str(intent.get("final_raw_sha256", "")):
            _raise("COMPLETED_CONTENT_CHANGED")
        return _completed_response(intent=intent, receipt=receipt, idempotent=True)
    if status in {"failed", "cancelled", "expired"}:
        _raise("INTENT_EXPIRED" if status == "expired" else "INTENT_NOT_APPLICABLE")

    if status in {"pending", "approved"}:
        try:
            write_intent.approve_intent(
                proposal_id,
                actor=ACTOR,
                raw_session_id=raw_session_id,
                target=target.path,
                proposal_raw_sha256=digest.raw_sha256,
                proposal_canonical_sha256=digest.canonical_sha256,
                approved_by="user",
                approval_ref=confirmation_ref,
            )
        except write_intent.IntentError as exc:
            _raise(exc.reason_code)
        intent = _authorized_intent(proposal_id, raw_session_id=raw_session_id)
        status = str(intent.get("status", ""))
    elif status in {"bound", "validated"} and not _approval_matches(intent, confirmation_ref):
        _raise("APPROVAL_ALREADY_BOUND")

    if status in {"pending", "approved"}:
        try:
            memory_claim.claim_paths(ACTOR, raw_session_id, [str(target.path)], proposal_id)
        except write_intent.IntentError as exc:
            _raise(exc.reason_code, retryable=exc.reason_code == "ACTIVE_TARGET_CONFLICT")
        except (OSError, ValueError) as exc:
            raise StudioWriteError("CLAIM_FAILED", "无法认领目标记忆文件。", retryable=True) from exc
        intent = _authorized_intent(proposal_id, raw_session_id=raw_session_id)
        status = str(intent.get("status", ""))

    if status not in {"bound", "validated"}:
        _raise("INTENT_NOT_APPLICABLE")
    if not _claim_matches(proposal_id, raw_session_id=raw_session_id, target=target.path):
        _raise("CLAIM_MISSING_FOR_RECOVERY")

    exists, current = _target_digest(target)
    if exists:
        _validate_studio_markdown(
            current.text,
            path=target.path,
            app_id=app_id,
            project_id=project_id,
            require_explicit_write_scope=False,
        )
    try:
        current_git_head = write_intent.current_git_head(required=True)
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    proposal_already_written = exists and current.raw_sha256 == digest.raw_sha256
    base_matches = (
        int(intent.get("base_exists", 0)) == int(exists)
        and current.raw_sha256 == str(intent.get("base_raw_sha256", ""))
        and current.canonical_sha256 == str(intent.get("base_canonical_sha256", ""))
        and current_git_head == str(intent.get("base_git_head", ""))
    )
    if status == "validated" and not proposal_already_written:
        _raise("TARGET_CHANGED_AFTER_CLAIM")
    if not proposal_already_written:
        if not base_matches:
            _terminalize_safe_concurrent_failure(
                proposal_id,
                raw_session_id=raw_session_id,
                target=target.path,
            )
            _raise("TARGET_CHANGED_AFTER_CLAIM", retryable=True)
        try:
            _atomic_conditional_write(
                target,
                proposal,
                proposal_id=proposal_id,
                expected_exists=bool(intent.get("base_exists", 0)),
                expected_raw_sha256=str(intent.get("base_raw_sha256", "")),
            )
        except StudioWriteError as exc:
            if exc.reason_code == "TARGET_CHANGED_AFTER_CLAIM":
                _terminalize_safe_concurrent_failure(
                    proposal_id,
                    raw_session_id=raw_session_id,
                    target=target.path,
                )
            raise
        exists, current = _target_digest(target)
        if not exists or current.raw_sha256 != digest.raw_sha256:
            _terminalize_safe_concurrent_failure(
                proposal_id,
                raw_session_id=raw_session_id,
                target=target.path,
            )
            _raise("TARGET_CHANGED_AFTER_CLAIM", retryable=True)

    _run_closeout(raw_session_id=raw_session_id, timeout_seconds=closeout_timeout)
    shown = write_intent.show_intent(proposal_id)
    completed_intent = shown.get("intent")
    completed_receipt = shown.get("receipt")
    if (
        not isinstance(completed_intent, dict)
        or not isinstance(completed_receipt, dict)
        or str(completed_intent.get("status", "")) != "completed"
        or str(completed_receipt.get("outcome", "")) != "completed"
    ):
        _raise("CLOSEOUT_FAILED")
    exists, current = _target_digest(target)
    if not exists or current.raw_sha256 != digest.raw_sha256:
        _raise("COMPLETED_CONTENT_CHANGED")
    return _completed_response(
        intent=completed_intent,
        receipt=completed_receipt,
        idempotent=proposal_already_written,
    )


def cancel(request: dict[str, Any], *, raw_session_id: str) -> dict[str, Any]:
    proposal_id = _required_string(request, "proposal_id", max_chars=64)
    if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        _raise("REQUEST_INVALID")
    intent = _authorized_intent(proposal_id, raw_session_id=raw_session_id)
    status = str(intent.get("status", ""))
    if status == "cancelled":
        try:
            target = _formal_target(str(intent.get("target_rel_path", "")))
            memory_claim.complete_claim_paths(raw_session_id, ACTOR, [target.path])
        except (StudioWriteError, OSError, sqlite3.Error, ValueError) as exc:
            raise StudioWriteError(
                "CLAIM_RELEASE_FAILED",
                "提案已取消，但会话认领未能释放，请重试取消。",
                retryable=True,
            ) from exc
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "stage": "cancel",
            "status": "cancelled",
            "proposal_id": proposal_id,
            "idempotent": True,
        }
    if status not in write_intent.ACTIVE_STATUSES:
        _raise("INTENT_NOT_CANCELLABLE")
    target = _formal_target(str(intent.get("target_rel_path", "")))
    if status in {"bound", "validated"}:
        exists, current = _target_digest(target)
        base_unchanged = (
            int(intent.get("base_exists", 0)) == int(exists)
            and current.raw_sha256 == str(intent.get("base_raw_sha256", ""))
        )
        if not base_unchanged:
            _raise("APPLY_RECOVERY_REQUIRED", retryable=True)
    try:
        write_intent.cancel_intent(
            proposal_id,
            actor=ACTOR,
            raw_session_id=raw_session_id,
            reason_code="CANCELLED_BY_USER",
        )
    except write_intent.IntentError as exc:
        _raise(exc.reason_code)
    try:
        memory_claim.complete_claim_paths(raw_session_id, ACTOR, [target.path])
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise StudioWriteError(
            "CLAIM_RELEASE_FAILED",
            "提案已取消，但会话认领未能释放，请重试取消。",
            retryable=True,
        ) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "stage": "cancel",
        "status": "cancelled",
        "proposal_id": proposal_id,
        "idempotent": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Two-phase host API for formal Agent Memory writes.",
        allow_abbrev=False,
    )
    parser.add_argument("--actor", choices=(ACTOR,), default=ACTOR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=15)
    parser.add_argument("--closeout-timeout", type=float, default=300)
    parser.add_argument("action", choices=("read-target", "prepare", "apply", "cancel"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        raw_session_id = _raw_session_id()
        request = _read_request()
        if args.action == "read-target":
            payload = read_target(request, raw_session_id=raw_session_id)
        else:
            with studio_write_lock(args.lock_timeout):
                if args.action == "prepare":
                    payload = prepare(request, raw_session_id=raw_session_id)
                elif args.action == "apply":
                    payload = apply(
                        request,
                        raw_session_id=raw_session_id,
                        closeout_timeout=max(float(args.closeout_timeout), 1),
                    )
                else:
                    payload = cancel(request, raw_session_id=raw_session_id)
    except StudioWriteError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "stage": getattr(args, "action", "unknown"),
            "status": "blocked",
            "reason_code": exc.reason_code,
            "message": exc.safe_message,
            "retryable": exc.retryable,
        }
    except (OSError, ValueError, write_intent.IntentError) as exc:
        reason_code = _safe_code(str(getattr(exc, "reason_code", "WRITE_INTERNAL_ERROR")))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "stage": getattr(args, "action", "unknown"),
            "status": "blocked",
            "reason_code": reason_code,
            "message": _error_message(reason_code),
            "retryable": False,
        }
    except Exception:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "stage": getattr(args, "action", "unknown"),
            "status": "blocked",
            "reason_code": "WRITE_INTERNAL_ERROR",
            "message": "正式记忆写入内部异常，未继续执行。",
            "retryable": False,
        }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
