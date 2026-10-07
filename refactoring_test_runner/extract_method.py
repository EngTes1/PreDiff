import re

from pathlib import Path
from typing import Any, Dict

from refactoring_test_runner.discovery import TestCase
from refactoring_test_runner.extract_variable import find_code_text, java_code_mask


DEFAULT_EXTRACTED_METHOD_NAME = "extractedMethod"
EXTRACT_METHOD_MARKER = re.compile(
    r"Refactoring\s+operation\s*:\s*(?:Extract|Introduce)\s+Method",
    re.IGNORECASE,
)


def extract_method_options(
    test_case: TestCase,
    operation: Dict[str, Any],
    *,
    preserve_newlines: bool = False,
) -> Dict[str, Any]:
    """Resolve the exact editor selection used by an Extract Method action."""
    java_file = test_case.java_files[0] if test_case.java_files else Path("")
    source = _read_source(java_file, preserve_newlines) if java_file.is_file() else ""
    start = _int_value(operation.get("target_start"), -1)
    length = _int_value(operation.get("target_length"), 0)
    selected_text = _selected_text(operation)
    occurrence = max(1, _int_value(operation.get("target_occurrence"), 1))

    marker = EXTRACT_METHOD_MARKER.search(source)
    anchor = _line_after(source, marker.end()) if marker else 0
    if start < 0 and selected_text:
        match = find_code_text(source, selected_text, anchor, occurrence)
        if match is None and anchor:
            match = find_code_text(source, selected_text, 0, occurrence)
        if match is not None:
            start, end = match
            length = end - start
    if start < 0 and marker:
        inferred = _next_statement_range(source, anchor)
        if inferred is not None:
            start, end = inferred
            length = end - start

    valid = 0 <= start < len(source) and length > 0 and start + length <= len(source)
    resolved_text = source[start:start + length] if valid else ""
    new_name = str(
        operation.get("new_name")
        or operation.get("method_name")
        or operation.get("extracted_method_name")
        or DEFAULT_EXTRACTED_METHOD_NAME
    ).strip() or DEFAULT_EXTRACTED_METHOD_NAME
    replace_duplicates = _bool_value(
        operation.get("replace_duplicates", operation.get("replace_all", False))
    )
    return {
        "target_start": start,
        "target_length": length,
        "selected_text": selected_text,
        "resolved_text": resolved_text,
        "selection_valid": valid,
        "new_name": new_name,
        "replace_duplicates": replace_duplicates,
    }


def _selected_text(operation: Dict[str, Any]) -> str:
    for key in (
        "selected_code",
        "target_code",
        "target_statements",
        "selected_statements",
        "target_expression",
        "selected_expression",
    ):
        value = operation.get(key)
        if isinstance(value, list):
            value = "\n".join(str(item) for item in value)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _next_statement_range(source: str, offset: int) -> tuple[int, int] | None:
    mask = java_code_mask(source)
    start = max(0, min(offset, len(mask)))
    while start < len(mask) and mask[start].isspace():
        start += 1
    if start >= len(mask):
        return None
    parentheses = brackets = braces = 0
    saw_brace = False
    for index in range(start, len(mask)):
        char = mask[index]
        if char == "(":
            parentheses += 1
        elif char == ")":
            parentheses = max(0, parentheses - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "{":
            braces += 1
            saw_brace = True
        elif char == "}":
            if braces == 0:
                return None
            braces -= 1
            if saw_brace and braces == 0 and parentheses == 0 and brackets == 0:
                return start, index + 1
        elif char == ";" and parentheses == 0 and brackets == 0 and braces == 0:
            return start, index + 1
    return None


def _line_after(source: str, offset: int) -> int:
    newline = source.find("\n", offset)
    return len(source) if newline < 0 else newline + 1


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _read_source(path: Path, preserve_newlines: bool) -> str:
    newline = "" if preserve_newlines else None
    with path.open("r", encoding="utf-8", newline=newline) as stream:
        return stream.read()
