import json
import re

from pathlib import Path
from typing import Any, List

from precondition_agent.language_rules.kotlin_rules import KOTLIN_FEATURE_PATTERNS


def load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def extract_json(text: str) -> Any:
    text = strip_code_fence(text)

    try:
        return json.loads(text)
    except Exception:
        pass

    candidates: List[str] = []
    for left, right in [("{", "}"), ("[", "]")]:
        first = text.find(left)
        last = text.rfind(right)
        if first != -1 and last != -1 and last > first:
            candidates.append(text[first:last + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue

    raise ValueError("LLM output is not valid JSON")


def message_to_text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(item.get("text", ""))
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    return str(content)


def infer_entry_name(snippet: str) -> str:
    """Best-effort inference of the current method name from a Java or Kotlin snippet."""
    patterns = [
        r"\bfun\s+(?:<[^>]+>\s*)?(?:(?:[A-Za-z_][A-Za-z0-9_]*\.)+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^()]*\)\s*\{",
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^()]*\)\s*$",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, snippet)
        if matches:
            for name in matches:
                if name not in {"if", "for", "while", "switch", "catch", "return", "when"}:
                    return name
    return ""


def detect_java_features(snippet: str) -> List[str]:
    checks = [
        ("instanceof", r"\binstanceof\s+[A-Z][A-Za-z0-9_]*"),
        ("null_check", r"\b[A-Za-z_][A-Za-z0-9_.]*\s*(?:==|!=)\s*null\b"),
        ("boolean_return", r"\breturn\s+(?:true|false)\b"),
        ("try_catch", r"\btry\s*\{|\bcatch\s*\("),
        ("method_declaration", r"\b(?:public|private|protected|static|final|synchronized|default)\b[^{;=]*\("),
    ]
    features: List[str] = []
    for name, pattern in checks:
        if re.search(pattern, snippet, flags=re.DOTALL):
            features.append(name)
    return features


def detect_kotlin_features(snippet: str) -> List[str]:
    """Return Kotlin syntax features that matter for precondition interpretation."""
    if _has_strong_java_signal(snippet) and not _has_strong_kotlin_signal(snippet):
        return []
    features: List[str] = []
    for name, pattern in KOTLIN_FEATURE_PATTERNS:
        if re.search(pattern, snippet, flags=re.DOTALL):
            features.append(name)
    return features


def infer_snippet_language(snippet: str) -> str:
    """Best-effort language hint for prompts and trace output."""
    java_strong = _has_strong_java_signal(snippet)
    kotlin_strong = _has_strong_kotlin_signal(snippet)
    if java_strong and not kotlin_strong:
        return "java"
    if kotlin_strong:
        return "kotlin"

    kotlin_features = detect_kotlin_features(snippet)
    if kotlin_features and not java_strong:
        return "kotlin"
    if detect_java_features(snippet):
        return "java"
    return "java"


def _has_strong_java_signal(snippet: str) -> bool:
    """Detect syntax that is much more likely Java than Kotlin."""
    patterns = [
        r"\b(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?[A-Za-z_][A-Za-z0-9_<>, ?@\[\].]*\s+[A-Za-z_][A-Za-z0-9_]*\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{",
        r"\b(?:final\s+)?[A-Z][A-Za-z0-9_<>, ?@\[\].]*\s+[a-z_][A-Za-z0-9_]*\s*=",
        r"\binstanceof\b",
        r"\bnew\s+[A-Z][A-Za-z0-9_]*\s*\(",
        r";\s*(?:\n|$)",
    ]
    return any(re.search(pattern, snippet, flags=re.DOTALL) for pattern in patterns)


def _has_strong_kotlin_signal(snippet: str) -> bool:
    """Detect syntax that is much more likely Kotlin than Java."""
    patterns = [
        r"\b(?:override\s+|suspend\s+|private\s+|public\s+|internal\s+|protected\s+)*fun\s+",
        r"\b(?:val|var)\s+[A-Za-z_][A-Za-z0-9_]*\s*(?::|=)",
        r"\bas\?\s+[A-Z][A-Za-z0-9_]*",
        r"(?:\bis\b|!is\s+)\s+[A-Z][A-Za-z0-9_]*",
        r"\?:\s*return\b",
        r"\bwhen\s*(?:\(|\{)",
    ]
    return any(re.search(pattern, snippet, flags=re.DOTALL) for pattern in patterns)


def normalize_text_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"[^a-z0-9_ ]+", " ", normalized)
    normalized = re.sub(r"\s+", "_", normalized).strip("_")
    return normalized
