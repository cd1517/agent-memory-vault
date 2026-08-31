#!/usr/bin/env python3
"""Read verified, bounded Markdown excerpts from the formal memory vault.

Search indexes are candidate finders only.  Every candidate is canonicalized,
re-read as strict UTF-8, and filtered again from its current frontmatter before
any excerpt is returned.  This command never mutates Markdown or derived state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_memory_env import env_value, expand_path
import agent_memory_index as memory_index
import agent_memory_intent as memory_intent
import agent_memory_safety as memory_safety
import agent_memory_search as memory_search


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = expand_path(env_value("ROOT", str(RUNTIME_ROOT / "templates" / "vault")))
GIT_ROOT = expand_path(env_value("GIT_ROOT", str(VAULT_ROOT)))

ACTORS = ("codex", "claude", "human", "migration", "test", "yichen-content-studio")
FORMAL_MEMORY_TOP_LEVELS = {"用户记忆", "项目", "工作流", "决策", "agent"}
SUPPORTING_MEMORY_TYPES = {"routing", "directory_index", "template"}
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_EXCERPT_BYTES = 12 * 1024
MAX_RESULTS_HARD_LIMIT = 100
MAX_CANDIDATES_HARD_LIMIT = 2000
MAX_REQUEST_BYTES = 64 * 1024
MAX_QUERY_CHARS = 16_384
STUDIO_ACTOR = "yichen-content-studio"
STUDIO_APP_ID = "yichen-content-studio"


class RetrievalProtocolError(ValueError):
    """A root or invocation failure that prevents safe retrieval."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Candidate:
    raw_path: str
    rel_path: str
    rank: int

    @property
    def reference(self) -> str:
        raw = self.rel_path or self.raw_path
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def normalized_query(raw: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def query_sha256(query: str) -> str:
    return hashlib.sha256(normalized_query(query).encode("utf-8")).hexdigest()


def _normalized_values(raw: str) -> set[str]:
    return {
        unicodedata.normalize("NFKC", item.strip()).casefold()
        for item in raw.split(",")
        if item.strip()
    }


def _normalized_identifier(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw.strip()).casefold()


def validate_studio_scope_request(app_id: str, project_id: str) -> tuple[str, str]:
    """Return the canonical studio scope or fail before candidate discovery.

    The app boundary is fixed centrally.  A non-empty project selects exactly
    that project.  An omitted project is the creative/shared channel and may
    see only this app's global/shared/unscoped memories.
    """

    normalized_app = _normalized_identifier(app_id)
    if not normalized_app:
        raise RetrievalProtocolError("APP_ID_REQUIRED")
    if normalized_app != STUDIO_APP_ID:
        raise RetrievalProtocolError("APP_ID_UNSUPPORTED")
    normalized_project = _normalized_identifier(project_id)
    if normalized_project:
        if normalized_project in {"global", "shared"} or "," in normalized_project:
            raise RetrievalProtocolError("PROJECT_ID_INVALID")
    return STUDIO_APP_ID, normalized_project


def _field_matches(expected: str, actual: str) -> bool:
    wanted = unicodedata.normalize("NFKC", expected.strip()).casefold()
    return bool(wanted) and wanted in _normalized_values(actual)


def _bool_value(value: object) -> bool:
    return memory_index.as_text(value).strip().casefold() in {"1", "true", "yes", "on", "required"}


def _safe_warning(
    code: str,
    *,
    candidate: Candidate | None = None,
    relative_path: str = "",
    reason: str = "",
) -> dict[str, str]:
    warning = {"code": code}
    if relative_path:
        warning["relative_path"] = relative_path
    elif candidate is not None:
        warning["candidate_ref"] = candidate.reference
    if reason:
        warning["reason"] = re.sub(r"[^A-Z0-9_]+", "_", reason.upper()).strip("_")[:80]
    return warning


def _sorted_warnings(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(sorted((str(name), str(value)) for name, value in row.items()))
        unique.setdefault(key, row)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.get("code", ""),
            item.get("relative_path", ""),
            item.get("candidate_ref", ""),
            item.get("reason", ""),
        ),
    )


def validate_vault_root() -> Path:
    lexical = Path(os.path.abspath(os.path.expandvars(str(VAULT_ROOT.expanduser()))))
    try:
        metadata = lexical.lstat()
    except FileNotFoundError as exc:
        raise RetrievalProtocolError("VAULT_MISSING") from exc
    except OSError as exc:
        raise RetrievalProtocolError("VAULT_UNREADABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RetrievalProtocolError("VAULT_ROOT_SYMLINK")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RetrievalProtocolError("VAULT_NOT_DIRECTORY")
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        raise RetrievalProtocolError("VAULT_UNRESOLVABLE") from exc


def _search_namespace(
    query: str,
    limit: int,
    *,
    actor: str = "",
    app_id: str = "",
    project_id: str = "",
) -> Namespace:
    studio = actor == STUDIO_ACTOR
    return Namespace(
        query=query,
        limit=limit,
        no_zvec=True,
        no_log=True,
        force_rg=False,
        zvec_timeout=1,
        zvec_max_distance=0.72,
        rg_timeout=1,
        track="",
        memory_type="",
        project_id=project_id if studio and project_id else "",
        current_project=project_id if studio else "",
        cross_project=False,
        as_of="",
        user_id="",
        agent_id="",
        agent_scope="shared" if studio else "",
        app_id=app_id if studio else "",
        session_id="",
        status="",
        has_open_loop=False,
        # The current Markdown, not the possibly stale index row, decides
        # status and supporting-document eligibility below.
        include_inactive=True,
        include_supporting=True,
    )


def search_candidates(
    query: str,
    max_results: int,
    *,
    actor: str = "",
    app_id: str = "",
    project_id: str = "",
) -> tuple[list[Candidate], list[dict[str, str]]]:
    candidate_limit = min(max(max_results * 32, 128), MAX_CANDIDATES_HARD_LIMIT)
    warnings: list[dict[str, str]] = []
    try:
        rows, backend_warnings, all_failed, _backend_status = memory_search.run_search(
            _search_namespace(
                query,
                candidate_limit,
                actor=actor,
                app_id=app_id,
                project_id=project_id,
            )
        )
    except Exception as exc:  # candidate discovery degradation is non-fatal
        reference = hashlib.sha256(type(exc).__name__.encode("utf-8")).hexdigest()[:16]
        return [], [{"code": "SEARCH_BACKEND_FAILED", "warning_ref": reference}]

    for raw_warning in backend_warnings:
        normalized = str(raw_warning).casefold()
        code = "SEARCH_INDEX_MISSING" if "index missing" in normalized else "SEARCH_BACKEND_WARNING"
        warnings.append(
            {
                "code": code,
                "warning_ref": hashlib.sha256(str(raw_warning).encode("utf-8")).hexdigest()[:16],
            }
        )
    if all_failed:
        warnings.append({"code": "SEARCH_BACKENDS_UNAVAILABLE"})

    candidates = [
        Candidate(raw_path=str(row.path or ""), rel_path=str(row.rel_path or ""), rank=rank)
        for rank, row in enumerate(rows, 1)
    ]
    return candidates, warnings


def _read_regular_file(path: Path, max_bytes: int, *, oversize_code: str = "FILE_TOO_LARGE") -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetrievalProtocolError("FILE_NOT_REGULAR")
        if metadata.st_size > max_bytes:
            raise RetrievalProtocolError(oversize_code)
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
            raise RetrievalProtocolError(oversize_code)
        return payload
    finally:
        os.close(descriptor)


def _frontmatter_text(text: str) -> str:
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("---\n"):
        remainder = normalized[4:]
        delimiter = re.search(r"(?m)^---.*$", remainder)
        if delimiter is None or delimiter.group(0) != "---":
            raise RetrievalProtocolError("FRONTMATTER_INVALID")
        protected_keys = {
            "memory_type",
            "track",
            "app_id",
            "project_id",
            "status",
            "agent_scope",
            "verified_at",
            "valid_until",
            "verification_mode",
            "requires_live_verification",
        }
        seen: set[str] = set()
        for line in remainder[: delimiter.start()].splitlines():
            if not line or line.startswith((" ", "-", "#")) or ":" not in line:
                continue
            key = line.split(":", 1)[0].strip()
            if key in protected_keys and key in seen:
                raise RetrievalProtocolError("FRONTMATTER_DUPLICATE_KEY")
            seen.add(key)
    return normalized


def _body_without_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return ""
    after = end + 4
    if after < len(text) and text[after] == "\n":
        after += 1
    return text[after:]


def _preferred_excerpt(text: str) -> str:
    body = _body_without_frontmatter(text).strip()
    lines = body.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"^##\s+当前有效摘要\s*$", line.strip()):
            start = index + 1
            break
    if start is not None:
        captured: list[str] = []
        for line in lines[start:]:
            if re.match(r"^#{1,2}\s+", line):
                break
            captured.append(line)
        preferred = "\n".join(captured).strip()
        if preferred:
            return preferred
    return body


def _bounded_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return text, False
    marker = "\n…".encode("utf-8")
    available = max(max_bytes - len(marker), 0)
    truncated = payload[:available].decode("utf-8", errors="ignore").rstrip()
    if max_bytes >= len(marker):
        truncated += marker.decode("utf-8")
    return truncated, True


def _contains_secret(text: str) -> bool:
    normalized = memory_safety.normalize_for_detection(text)
    return any(pattern.search(normalized) for pattern in memory_safety.SECRET_PATTERNS)


def _metadata_for(path: Path, text: str) -> dict[str, Any]:
    meta = memory_index.parse_frontmatter(text)
    memory_type, track, _inferred_project_id, status = memory_index.infer_from_path(path, meta)
    verified_at, verified_at_source = memory_index.extract_verified_at(text, meta, memory_type, status)
    return {
        "meta": meta,
        "memory_type": memory_type,
        "track": track,
        # Missing project_id is an intentionally unscoped shared reference;
        # path-derived names are indexing aids, not an authorization scope.
        "project_id": memory_index.as_text(meta.get("project_id")),
        "status": status,
        "app_id": memory_index.as_text(meta.get("app_id"), memory_index.DEFAULT_APP_ID),
        "agent_scope": memory_index.as_text(meta.get("agent_scope"), "shared").casefold(),
        "verified_at": verified_at,
        "verified_at_source": verified_at_source,
        "valid_until": memory_index.as_text(meta.get("valid_until")),
        "verification_mode": memory_index.as_text(meta.get("verification_mode")).casefold(),
        "explicit_live_verification": _bool_value(meta.get("requires_live_verification")),
    }


def _scope_status(actual_project: str, requested_project: str) -> str:
    projects = _normalized_values(actual_project)
    if not projects:
        return "unscoped_shared_reference"
    if projects <= {"global", "shared"}:
        return "global_shared"
    if requested_project and _field_matches(requested_project, actual_project):
        return "current_project"
    return "other_project"


def _metadata_rejection(metadata: dict[str, Any], actor: str, app_id: str, project_id: str) -> str:
    if unicodedata.normalize("NFKC", str(metadata["status"])).casefold() != "active":
        return "STATUS_NOT_ACTIVE"
    agent_scope = str(metadata["agent_scope"])
    allowed_scopes = {"shared"}
    if actor in {"codex", "claude"}:
        allowed_scopes.add(actor)
    if agent_scope not in allowed_scopes:
        return "AGENT_SCOPE_MISMATCH"
    if actor == STUDIO_ACTOR:
        if _normalized_values(str(metadata["app_id"])) != {_normalized_identifier(app_id)}:
            return "APP_ID_MISMATCH"
    elif app_id and not _field_matches(app_id, str(metadata["app_id"])):
        return "APP_ID_MISMATCH"
    scope_status = _scope_status(str(metadata["project_id"]), project_id)
    if actor == STUDIO_ACTOR:
        # Project-scoped studio reads are exact.  The intentionally unscoped
        # creative channel can read only app-owned shared references.
        if project_id:
            actual_projects = _normalized_values(str(metadata["project_id"]))
            if actual_projects != {_normalized_identifier(project_id)}:
                return "PROJECT_SCOPE_MISMATCH"
        elif scope_status not in {"unscoped_shared_reference", "global_shared"}:
            return "PROJECT_SCOPE_MISMATCH"
    elif scope_status == "other_project":
        return "PROJECT_SCOPE_MISMATCH"
    if str(metadata["memory_type"]) in SUPPORTING_MEMORY_TYPES:
        return "SUPPORTING_DOCUMENT_EXCLUDED"
    return ""


def _live_verification(metadata: dict[str, Any], as_of: dt.date) -> tuple[dict[str, Any], list[str], str]:
    reasons: list[str] = []
    time_status = "unspecified"
    valid_until = str(metadata["valid_until"])
    if valid_until:
        try:
            boundary = dt.date.fromisoformat(valid_until[:10])
        except ValueError:
            time_status = "invalid"
            reasons.append("invalid_valid_until")
        else:
            if boundary < as_of:
                time_status = "expired"
                reasons.append("expired_memory_reference_only")
            elif boundary == as_of:
                time_status = "expires_today"
                reasons.append("memory_expires_today")
            else:
                time_status = "current"
    if str(metadata["verification_mode"]) == "needs_review" or str(metadata["verified_at_source"]) == "needs_review":
        reasons.append("verification_needed")
    if bool(metadata["explicit_live_verification"]):
        reasons.append("frontmatter_requires_live_verification")
    reasons = list(dict.fromkeys(reasons))
    return (
        {
            "required": bool(reasons),
            "reasons": reasons,
            "verification_mode": str(metadata["verification_mode"]),
        },
        reasons,
        time_status,
    )


def current_git_head() -> tuple[str, dict[str, str] | None]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(GIT_ROOT), "rev-parse", "--verify", "HEAD"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        reference = hashlib.sha256(type(exc).__name__.encode("utf-8")).hexdigest()[:16]
        return "", {"code": "GIT_HEAD_UNAVAILABLE", "warning_ref": reference}
    head = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", head):
        return "", {"code": "GIT_HEAD_UNAVAILABLE"}
    return head, None


def retrieve(
    *,
    actor: str,
    app_id: str,
    project_id: str,
    query: str,
    max_results: int,
    max_file_bytes: int,
    max_total_bytes: int,
    max_excerpt_bytes: int,
    candidates: list[Candidate] | None = None,
) -> dict[str, Any]:
    validate_vault_root()
    normalized = normalized_query(query)
    if not normalized:
        raise RetrievalProtocolError("QUERY_REQUIRED")
    if actor not in ACTORS:
        raise RetrievalProtocolError("ACTOR_UNSUPPORTED")
    if actor == STUDIO_ACTOR:
        app_id, project_id = validate_studio_scope_request(app_id, project_id)
    if not 1 <= max_results <= MAX_RESULTS_HARD_LIMIT:
        raise RetrievalProtocolError("MAX_RESULTS_INVALID")
    if min(max_file_bytes, max_total_bytes, max_excerpt_bytes) <= 0:
        raise RetrievalProtocolError("BYTE_LIMIT_INVALID")

    warnings: list[dict[str, str]] = []
    if candidates is None:
        candidates, search_warnings = search_candidates(
            normalized,
            max_results,
            actor=actor,
            app_id=app_id,
            project_id=project_id,
        )
        warnings.extend(search_warnings)

    git_head, git_warning = current_git_head()
    if git_warning:
        warnings.append(git_warning)

    results: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    bytes_inspected = 0
    as_of = dt.datetime.now().date()

    for candidate in candidates:
        if len(results) >= max_results:
            break
        raw_target = candidate.raw_path or candidate.rel_path
        try:
            target = memory_intent.canonical_target(raw_target)
        except memory_intent.IntentError as exc:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", candidate=candidate, reason=exc.reason_code))
            continue
        except OSError:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", candidate=candidate, reason="PATH_UNREADABLE"))
            continue
        if target.target_key in seen_targets:
            continue
        seen_targets.add(target.target_key)
        rel_path = target.rel_path
        relative = Path(rel_path)
        if not relative.parts or relative.parts[0] not in FORMAL_MEMORY_TOP_LEVELS:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="NON_FORMAL_PATH"))
            continue
        try:
            metadata = target.path.lstat()
        except FileNotFoundError:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="FILE_MISSING"))
            continue
        except OSError:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="FILE_UNREADABLE"))
            continue
        if stat.S_ISLNK(metadata.st_mode):
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="SYMLINK_FORBIDDEN"))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="FILE_NOT_REGULAR"))
            continue
        if metadata.st_size > max_file_bytes:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="FILE_TOO_LARGE"))
            continue
        if bytes_inspected + metadata.st_size > max_total_bytes:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="TOTAL_BYTE_BUDGET"))
            continue
        try:
            remaining_budget = max_total_bytes - bytes_inspected
            payload = _read_regular_file(
                target.path,
                min(max_file_bytes, remaining_budget),
                oversize_code="TOTAL_BYTE_BUDGET" if remaining_budget < max_file_bytes else "FILE_TOO_LARGE",
            )
        except RetrievalProtocolError as exc:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason=exc.code))
            continue
        except OSError:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="FILE_UNREADABLE"))
            continue
        bytes_inspected += len(payload)
        try:
            raw_text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="CONTENT_NOT_UTF8"))
            continue
        if _contains_secret(raw_text):
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason="SECRET_MATERIAL"))
            continue
        try:
            parsed_text = _frontmatter_text(raw_text)
            current_metadata = _metadata_for(target.path, parsed_text)
        except (RetrievalProtocolError, OSError, ValueError) as exc:
            reason = exc.code if isinstance(exc, RetrievalProtocolError) else "FRONTMATTER_INVALID"
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason=reason))
            continue
        rejection = _metadata_rejection(current_metadata, actor, app_id, project_id)
        if rejection:
            warnings.append(_safe_warning("CANDIDATE_REJECTED", relative_path=rel_path, reason=rejection))
            continue

        excerpt, excerpt_truncated = _bounded_utf8(_preferred_excerpt(parsed_text), max_excerpt_bytes)
        live_verification, policy_warnings, time_status = _live_verification(current_metadata, as_of)
        scope_status = _scope_status(str(current_metadata["project_id"]), project_id)
        policy = {
            "status": str(current_metadata["status"]),
            "agent_scope": str(current_metadata["agent_scope"]),
            "app_id": str(current_metadata["app_id"]),
            "project_id": str(current_metadata["project_id"]),
            "scope_status": scope_status,
            "valid_until": str(current_metadata["valid_until"]),
            "time_status": time_status,
            "warnings": policy_warnings,
            "can_authorize_action": False,
        }
        results.append(
            {
                "relative_path": rel_path,
                "sha256": sha256_bytes(payload),
                "verified_at": str(current_metadata["verified_at"]),
                "verified_at_source": str(current_metadata["verified_at_source"]),
                "git_head": git_head,
                "excerpt": excerpt,
                "excerpt_truncated": excerpt_truncated,
                "size_bytes": len(payload),
                "policy": policy,
                "live_verification": live_verification,
            }
        )

    return {
        "schema_version": 1,
        "ok": True,
        "actor": actor,
        "app_id": app_id,
        "project_id": project_id,
        "query_hash": query_sha256(normalized),
        "git_head": git_head,
        "retrieved_at": utc_now(),
        "result_count": len(results),
        "bytes_inspected": bytes_inspected,
        "limits": {
            "max_results": max_results,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_excerpt_bytes": max_excerpt_bytes,
        },
        "results": results,
        "warnings": _sorted_warnings(warnings),
    }


def _read_json_request() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(payload) > MAX_REQUEST_BYTES:
        raise RetrievalProtocolError("REQUEST_TOO_LARGE")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        request = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalProtocolError("REQUEST_INVALID") from exc
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise RetrievalProtocolError("REQUEST_INVALID")
    allowed = {
        "schema_version",
        "query",
        "app_id",
        "project_id",
        "max_results",
        "max_file_bytes",
        "max_total_bytes",
        "max_excerpt_bytes",
    }
    if set(request) - allowed:
        raise RetrievalProtocolError("REQUEST_INVALID")
    query = request.get("query")
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or "\x00" in query
        or any(ord(character) < 32 and character not in "\t\n\r" for character in query)
    ):
        raise RetrievalProtocolError("QUERY_REQUIRED")
    for key in ("app_id", "project_id"):
        value = request.get(key, "")
        if not isinstance(value, str) or len(value) > 160 or "\x00" in value:
            raise RetrievalProtocolError("REQUEST_INVALID")
    for key in ("max_results", "max_file_bytes", "max_total_bytes", "max_excerpt_bytes"):
        if key in request and (not isinstance(request[key], int) or isinstance(request[key], bool)):
            raise RetrievalProtocolError("REQUEST_INVALID")
    return request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read bounded excerpts from current canonical Agent Memory Markdown."
    )
    parser.add_argument("query", nargs="?", help="Legacy non-studio query; studio queries must use stdin JSON.")
    parser.add_argument("--query", dest="query_option", help="Legacy alternative to the positional query.")
    parser.add_argument("--actor", choices=ACTORS, default=os.environ.get("MEMORY_ACTOR", "codex"))
    parser.add_argument("--app-id", default="")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-excerpt-bytes", type=int, default=DEFAULT_MAX_EXCERPT_BYTES)
    parser.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    args = parser.parse_args()
    args.query = args.query_option or args.query or ""
    if args.actor == STUDIO_ACTOR:
        if args.query or args.app_id or args.project_id:
            raise RetrievalProtocolError("QUERY_STDIN_REQUIRED")
        request = _read_json_request()
        args.query = str(request["query"])
        args.app_id = str(request.get("app_id", ""))
        args.project_id = str(request.get("project_id", ""))
        for key in ("max_results", "max_file_bytes", "max_total_bytes", "max_excerpt_bytes"):
            if key in request:
                setattr(args, key, int(request[key]))
    return args


def main() -> int:
    try:
        args = parse_args()
        payload = retrieve(
            actor=args.actor,
            app_id=args.app_id,
            project_id=args.project_id,
            query=args.query,
            max_results=args.max_results,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
            max_excerpt_bytes=args.max_excerpt_bytes,
        )
    except RetrievalProtocolError as exc:
        payload = {"schema_version": 1, "ok": False, "error": {"code": exc.code}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except Exception:
        payload = {"schema_version": 1, "ok": False, "error": {"code": "RETRIEVAL_FAILED"}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
