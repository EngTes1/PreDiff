import re

from pathlib import Path
from typing import Any, Dict

from refactoring_test_runner.discovery import TestCase


DEFAULT_EXTRACTED_VARIABLE_NAME = "extractedValue"
EXTRACT_VARIABLE_MARKER = re.compile(
    r"Refactoring\s+operation\s*:\s*(?:Extract|Introduce)\s+(?:Local\s+)?Variable",
    re.IGNORECASE,
)
QUOTED_TEXT = re.compile(r"['\"]([^'\"\r\n]+)['\"]")


def extract_variable_options(
    test_case: TestCase,
    operation: Dict[str, Any],
    *,
    preserve_newlines: bool = False,
) -> Dict[str, Any]:
    """Resolve a deterministic source selection for an Extract Variable operation."""
    java_file = test_case.java_files[0] if test_case.java_files else Path("")
    source = _read_source(java_file, preserve_newlines=preserve_newlines) if java_file.is_file() else ""
    hint = str(operation.get("target_location_hint") or operation.get("target_hint") or "")

    explicit_start = _non_negative_int(operation.get("target_start"), default=-1)
    explicit_length = _non_negative_int(operation.get("target_length"), default=0)
    expression = _explicit_expression(operation)
    target_occurrence = max(
        1,
        _non_negative_int(
            operation.get("target_occurrence", operation.get("occurrence_index", 1)),
            default=1,
        ),
    )

    marker = EXTRACT_VARIABLE_MARKER.search(source)
    anchor = _line_after(source, marker.end()) if marker else 0
    if not expression:
        expression = _expression_from_hint(hint)
    if not expression and marker:
        marker_line_end = source.find("\n", marker.end())
        marker_line = source[marker.start(): marker_line_end if marker_line_end >= 0 else len(source)]
        expression = _quoted_expression(marker_line)
    start = explicit_start
    length = explicit_length
    if start < 0 and expression:
        match = _find_expression(source, expression, anchor, target_occurrence)
        if not match and anchor:
            match = _find_expression(source, expression, 0, target_occurrence)
        if match:
            start, end = match
            length = end - start
    if start < 0:
        start = _next_code_offset(source, anchor)
        length = 0

    new_name = str(
        operation.get("new_name")
        or operation.get("variable_name")
        or operation.get("target_name")
        or DEFAULT_EXTRACTED_VARIABLE_NAME
    ).strip()
    replace_all = _as_bool(
        operation.get("replace_all")
        or operation.get("replace_all_occurrences")
        or operation.get("replace_all_in_file")
    )
    return {
        "target_start": start,
        "target_length": length,
        "target_expression": expression,
        "target_occurrence": target_occurrence,
        "new_name": new_name,
        "replace_all": replace_all,
    }


def _explicit_expression(operation: Dict[str, Any]) -> str:
    for key in ("target_expression", "selected_expression", "expression"):
        value = str(operation.get(key) or "").strip()
        if value:
            return value
    return ""


def _quoted_expression(text: str) -> str:
    candidates = [match.group(1).strip() for match in QUOTED_TEXT.finditer(text)]
    candidates = [item for item in candidates if item]
    return candidates[-1] if candidates else ""


def _expression_from_hint(hint: str) -> str:
    if re.search(r"\bnull\s+literal\b", hint, re.IGNORECASE):
        return "null"
    if re.search(r"\barray\s+initializer\b", hint, re.IGNORECASE):
        first = hint.find("{")
        if first >= 0:
            depth = 0
            for index in range(first, len(hint)):
                if hint[index] == "{":
                    depth += 1
                elif hint[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return hint[first:index + 1]
    return _quoted_expression(hint)


def _find_expression(
    source: str,
    expression: str,
    start: int,
    occurrence: int = 1,
) -> tuple[int, int] | None:
    code_mask = java_code_mask(source)
    search_from = start
    for _ in range(max(1, occurrence)):
        exact = source.find(expression, search_from)
        while exact >= 0 and not _range_is_code(code_mask, exact, exact + len(expression)):
            exact = source.find(expression, exact + 1)
        if exact < 0:
            break
        search_from = exact + len(expression)
    else:
        return exact, exact + len(expression)
    compact = "".join(expression.split())
    if not compact:
        return None
    pattern = r"\s*".join(re.escape(ch) for ch in compact)
    matches = [
        match for match in re.finditer(pattern, source[start:])
        if _range_is_code(code_mask, start + match.start(), start + match.end())
    ]
    if len(matches) < occurrence:
        return None
    match = matches[occurrence - 1]
    absolute_start = start + match.start()
    return absolute_start, start + match.end()


def java_code_mask(source: str) -> str:
    """Preserve offsets while blanking comments and literal contents."""
    chars = list(source)
    index = 0
    state = "code"
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if source.startswith('\"\"\"', index):
                index += 3
                state = "text_block"
                continue
            if current == '"':
                index += 1
                state = "string"
                continue
            if current == "'":
                index += 1
                state = "char"
                continue
            index += 1
            continue
        if state == "line_comment":
            if current in "\r\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
            else:
                if current not in "\r\n":
                    chars[index] = " "
                index += 1
            continue
        if state == "text_block":
            if source.startswith('\"\"\"', index):
                index += 3
                state = "code"
            else:
                if current not in "\r\n":
                    chars[index] = " "
                index += 1
            continue
        if current == "\\" and index + 1 < len(chars):
            chars[index] = chars[index + 1] = " "
            index += 2
            continue
        if (state == "string" and current == '"') or (state == "char" and current == "'"):
            state = "code"
        else:
            chars[index] = " "
        index += 1
    return "".join(chars)


def find_code_text(source: str, text: str, start: int = 0, occurrence: int = 1) -> tuple[int, int] | None:
    return _find_expression(source, text, start, occurrence)


def _range_is_code(mask: str, start: int, end: int) -> bool:
    return any(not mask[index].isspace() for index in range(max(0, start), min(len(mask), end)))


def _line_after(source: str, offset: int) -> int:
    newline = source.find("\n", offset)
    return len(source) if newline < 0 else newline + 1


def _next_code_offset(source: str, offset: int) -> int:
    index = max(0, min(offset, len(source)))
    while index < len(source) and source[index].isspace():
        index += 1
    return index


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _read_source(path: Path, *, preserve_newlines: bool) -> str:
    # Eclipse buffers preserve CRLF; IntelliJ/NetBeans editor documents normalize it.
    newline = "" if preserve_newlines else None
    with path.open("r", encoding="utf-8", newline=newline) as stream:
        return stream.read()
