"""Filesystem boundary helpers for user-controlled paths."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable


def _is_within(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath([root, candidate]) == root
    except ValueError:
        return False


def ensure_within_root(
    root: str,
    candidate: str,
    *,
    reject_symlinks: bool = True,
) -> str:
    """Return a canonical path only when it remains inside ``root``."""
    root_real = os.path.realpath(os.path.abspath(root))
    candidate_abs = os.path.abspath(candidate)
    candidate_real = os.path.realpath(candidate_abs)
    if not _is_within(root_real, candidate_real):
        raise ValueError("文件路径超出允许目录")

    if reject_symlinks:
        relative = os.path.relpath(candidate_abs, root_real)
        current = root_real
        if relative != ".":
            for part in relative.split(os.sep):
                current = os.path.join(current, part)
                if os.path.islink(current):
                    raise ValueError("文件路径不得经过符号链接")
    return candidate_real


def safe_child_path(
    root: str,
    relative_path: str,
    *,
    allowed_suffixes: Iterable[str] = (),
    filename_only: bool = False,
) -> str:
    """Resolve a relative child path without traversal or symlink escapes."""
    raw = str(relative_path or "").strip()
    if not raw or "\x00" in raw or os.path.isabs(raw):
        raise ValueError("必须提供允许目录内的相对文件路径")

    normalized = os.path.normpath(raw)
    if normalized in {".", ".."} or normalized.startswith(f"..{os.sep}"):
        raise ValueError("文件路径不得包含上级目录")
    if filename_only and os.path.basename(normalized) != normalized:
        raise ValueError("文件名不得包含目录")

    suffixes = tuple(item.lower() for item in allowed_suffixes)
    if suffixes and not normalized.lower().endswith(suffixes):
        raise ValueError(f"文件类型仅允许: {', '.join(suffixes)}")

    root_real = os.path.realpath(os.path.abspath(root))
    return ensure_within_root(root_real, os.path.join(root_real, normalized))


def ensure_allowed_path(candidate: str, allowed_roots: Iterable[str]) -> str:
    """Resolve an existing/configured path under one of the explicit roots."""
    candidate_abs = os.path.abspath(os.path.expanduser(str(candidate or "").strip()))
    for root in allowed_roots:
        root_real = os.path.realpath(os.path.abspath(root))
        candidate_real = os.path.realpath(candidate_abs)
        if _is_within(root_real, candidate_real):
            return ensure_within_root(root_real, candidate_abs)
    raise ValueError("路径不在允许目录内")


def safe_identifier(value: str, *, label: str = "标识符", max_length: int = 128) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} 无效")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        raise ValueError(f"{label} 只能包含字母、数字、下划线和连字符")
    return normalized
