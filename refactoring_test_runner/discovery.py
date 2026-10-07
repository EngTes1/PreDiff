import fnmatch
import json
import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class TestCase:
    test_id: str
    case_dir: Path
    java_files: List[Path]
    main_class: str
    source_index: Path
    metadata: Dict[str, Any]


def discover_test_cases(test_root: str | Path, include: str = "*") -> List[TestCase]:
    root = Path(test_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"test root not found: {root}")

    index_paths = _find_index_paths(root)
    cases: List[TestCase] = []
    for index_path in index_paths:
        cases.extend(_cases_from_index(index_path, include=include))

    if not cases:
        cases.extend(_fallback_cases_from_java_files(root, include=include))

    return sorted(cases, key=lambda item: item.test_id)


def _find_index_paths(root: Path) -> List[Path]:
    if root.is_file() and root.name == "test_cases.json":
        return [root]
    if (root / "test_cases.json").exists():
        return [root / "test_cases.json"]
    return sorted(root.rglob("test_cases.json"))


def _cases_from_index(index_path: Path, include: str = "*") -> List[TestCase]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []
    base = index_path.parent.resolve()
    cases: List[TestCase] = []
    for raw in payload.get("test_cases", []) or []:
        if not isinstance(raw, dict):
            continue
        test_id = str(raw.get("test_id", "")).strip()
        if not test_id or not fnmatch.fnmatch(test_id, include):
            continue
        case_dir = (base / test_id).resolve()
        files = _java_files_from_case(case_dir, raw)
        if not files:
            continue
        main_class = _main_class_from_case(raw, files[0])
        cases.append(
            TestCase(
                test_id=test_id,
                case_dir=case_dir,
                java_files=files,
                main_class=main_class,
                source_index=index_path,
                metadata=raw,
            )
        )
    return cases


def _java_files_from_case(case_dir: Path, raw: Dict[str, Any]) -> List[Path]:
    files: List[Path] = []
    for item in raw.get("files", []) or []:
        if not isinstance(item, dict):
            continue
        rel = Path(str(item.get("path", "")))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        path = (case_dir / rel).resolve()
        if path.exists() and path.suffix == ".java":
            files.append(path)
    if files:
        return files
    return sorted(case_dir.glob("src/*.java")) + sorted(case_dir.glob("src/main/java/*.java"))


def _main_class_from_case(raw: Dict[str, Any], java_file: Path) -> str:
    for item in raw.get("files", []) or []:
        if isinstance(item, dict) and str(item.get("main_class", "")).strip():
            return str(item.get("main_class", "")).strip()
    package_name = _package_name(java_file)
    if package_name:
        return f"{package_name}.{java_file.stem}"
    return java_file.stem


def _fallback_cases_from_java_files(root: Path, include: str = "*") -> List[TestCase]:
    cases: List[TestCase] = []
    java_files = (
        sorted(root.glob("*/src/**/*.java"))
        + sorted(root.glob("*/src/main/java/**/*.java"))
    )
    for java_file in java_files:
        test_id = java_file.stem
        if not fnmatch.fnmatch(test_id, include):
            continue
        if "src" in java_file.parts:
            src_index = java_file.parts.index("src")
            case_dir = Path(*java_file.parts[:src_index])
        else:
            case_dir = java_file.parent.parent
        case_dir = case_dir.resolve()
        package_name = _package_name(java_file)
        main_class = f"{package_name}.{test_id}" if package_name else test_id
        cases.append(
            TestCase(
                test_id=test_id,
                case_dir=case_dir,
                java_files=[java_file],
                main_class=main_class,
                source_index=Path(""),
                metadata={},
            )
        )
    return cases


def _package_name(java_file: Path) -> str:
    try:
        text = java_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    return match.group(1) if match else ""
