import re

from pathlib import Path
from typing import Any, Dict

from refactoring_test_runner.discovery import TestCase
from refactoring_test_runner.extract_variable import find_code_text, java_code_mask


def inline_method_options(
    test_case: TestCase,
    operation: Dict[str, Any],
    *,
    preserve_newlines: bool,
) -> Dict[str, Any]:
    """Resolve the exact editor selection for an Inline Method invocation."""
    java_file = test_case.java_files[0] if test_case.java_files else Path("")
    source = _read_source(java_file, preserve_newlines) if java_file.is_file() else ""
    target_method = str(operation.get("target_method") or "").strip()

    explicit_start = _integer(operation.get("target_start"), -1)
    explicit_length = _integer(operation.get("target_length"), 0)
    if _valid_range(source, explicit_start, explicit_length):
        return _result(source, explicit_start, explicit_length, "explicit_range")

    occurrence = max(1, _integer(operation.get("target_occurrence") or operation.get("occurrence"), 1))
    for key in ("target_invocation", "target_call", "selected_code", "target_symbol"):
        text = str(operation.get(key) or "").strip()
        if not _looks_like_invocation(text, target_method):
            continue
        match = find_code_text(source, text, occurrence=occurrence)
        if match is not None:
            start, end = match
            return _result(source, start, end - start, key)

    marked = _marked_invocation(source, target_method)
    if marked is not None:
        return _result(source, marked[0], marked[1], "inline_marker")

    unique = _unique_invocation(source, target_method)
    if unique is not None:
        return _result(source, unique[0], unique[1], "unique_invocation")

    return {
        "target_start": -1,
        "target_length": 0,
        "selection_valid": False,
        "selection_source": "unresolved",
        "resolved_text": "",
        "preserve_newlines": preserve_newlines,
    }


def _result(source: str, start: int, length: int, selection_source: str) -> Dict[str, Any]:
    return {
        "target_start": start,
        "target_length": length,
        "selection_valid": True,
        "selection_source": selection_source,
        "resolved_text": source[start:start + length],
    }


def _valid_range(source: str, start: int, length: int) -> bool:
    return start >= 0 and length > 0 and start + length <= len(source)


def _looks_like_invocation(text: str, target_method: str) -> bool:
    if not text:
        return False
    if "::" in text or "(" in text:
        return not target_method or target_method in text
    return False


def _marked_invocation(source: str, target_method: str) -> tuple[int, int] | None:
    if not target_method:
        return None
    mask = java_code_mask(source)
    invocation = re.compile(rf"(?:[A-Za-z_$][\w$]*\s*\.\s*)*{re.escape(target_method)}\s*\([^;{{}}]*\)")
    offset = 0
    pending_marker = False
    for line in source.splitlines(keepends=True):
        lower = line.lower()
        marker = ("inline" in lower and ("call" in lower or "invocation" in lower or "target" in lower))
        code_line = mask[offset:offset + len(line)]
        matches = list(invocation.finditer(code_line))
        if matches and (marker or pending_marker):
            match = matches[-1] if marker else matches[0]
            return offset + match.start(), match.end() - match.start()
        pending_marker = marker and not matches
        if line.strip() and not line.lstrip().startswith("//") and not matches:
            pending_marker = False
        offset += len(line)
    return None


def _unique_invocation(source: str, target_method: str) -> tuple[int, int] | None:
    if not target_method:
        return None
    mask = java_code_mask(source)
    pattern = re.compile(rf"(?:[A-Za-z_$][\w$]*\s*\.\s*)*{re.escape(target_method)}\s*\([^;{{}}]*\)")
    candidates: list[tuple[int, int]] = []
    for match in pattern.finditer(mask):
        prefix = mask[max(0, match.start() - 40):match.start()]
        if re.search(r"\b(?:void|boolean|byte|short|int|long|float|double|char|String|[A-Z][\w$<>?, ]*)\s+$", prefix):
            continue
        candidates.append((match.start(), match.end() - match.start()))
    return candidates[0] if len(candidates) == 1 else None


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_source(path: Path, preserve_newlines: bool) -> str:
    newline = "" if preserve_newlines else None
    with path.open("r", encoding="utf-8", newline=newline) as stream:
        return stream.read()
