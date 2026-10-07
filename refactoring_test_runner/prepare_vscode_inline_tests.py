"""Prepare Inline Method test cases for the VS Code Java/JDT LS runner.

The formal test cases are kept unchanged. This script copies a generated
test root into a VS Code Java specific root and normalizes Java files into a
named package. JDT LS refactorings are less stable for default-package files,
so the packaged copy is used only for VS Code Java experiments.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


DEFAULT_PACKAGE = "generated.refactoring.tests"


PACKAGE_RE = re.compile(r"(?m)^\s*package\s+[\w.]+\s*;\s*\n+")


def normalize_java_source(source: str, package_name: str) -> str:
    """Return source with exactly one package declaration at the top."""
    body = PACKAGE_RE.sub("", source, count=1).lstrip()
    return f"package {package_name};\n\n{body}"


def class_name_from_path(path_text: str) -> str:
    return Path(path_text).stem


def load_file_content(case_dir: Path, file_meta: dict[str, Any]) -> str:
    file_path = case_dir / file_meta["path"]
    if file_path.exists():
        return file_path.read_text(encoding="utf-8")
    content = file_meta.get("content")
    if isinstance(content, str):
        return content
    raise FileNotFoundError(f"Missing Java source and embedded content: {file_path}")


def prepare_root(source_root: Path, output_root: Path, package_name: str, force: bool) -> dict[str, Any]:
    test_cases_file = source_root / "test_cases.json"
    if not test_cases_file.exists():
        raise FileNotFoundError(f"Missing test_cases.json: {test_cases_file}")

    if output_root.exists():
        if not force:
            raise FileExistsError(f"Output root already exists: {output_root}")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    data = json.loads(test_cases_file.read_text(encoding="utf-8"))
    test_cases = data.get("test_cases", [])
    if not isinstance(test_cases, list):
        raise ValueError(f"Invalid test_cases array: {test_cases_file}")

    prepared_count = 0
    for test_case in test_cases:
        test_id = test_case.get("test_id")
        if not isinstance(test_id, str) or not test_id:
            raise ValueError(f"Invalid test_id in {test_cases_file}")

        source_case_dir = source_root / test_id
        output_case_dir = output_root / test_id
        output_src_dir = output_case_dir / "src" / Path(*package_name.split("."))
        output_src_dir.mkdir(parents=True, exist_ok=True)

        files = test_case.get("files", [])
        if not isinstance(files, list) or not files:
            raise ValueError(f"Missing files metadata for {test_id}")

        for file_meta in files:
            if not isinstance(file_meta, dict) or "path" not in file_meta:
                raise ValueError(f"Invalid file metadata for {test_id}")

            class_name = class_name_from_path(str(file_meta["path"]))
            output_rel = Path("src") / Path(*package_name.split(".")) / f"{class_name}.java"
            content = load_file_content(source_case_dir, file_meta)
            normalized = normalize_java_source(content, package_name)
            (output_case_dir / output_rel).write_text(normalized, encoding="utf-8")

            file_meta["path"] = output_rel.as_posix()
            file_meta["main_class"] = f"{package_name}.{class_name}"
            file_meta["content"] = normalized

        test_case["vscode_java_package_normalized"] = True
        prepared_count += 1

    policy = data.setdefault("generation_policy", {})
    if isinstance(policy, dict):
        policy["output_layout"] = f"<output_root>/<test_id>/src/{package_name.replace('.', '/')}/<test_id>.java"
        policy["project_layout"] = "simple-packaged"

    data["vscode_java_package_normalized"] = {
        "package": package_name,
        "source_root": str(source_root),
        "reason": "Avoid default-package instability in JDT LS refactoring commands.",
    }

    (output_root / "test_cases.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "source_root": str(source_root),
        "output_root": str(output_root),
        "test_cases": prepared_count,
        "package": package_name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", action="append", required=True, help="Source generated test root.")
    parser.add_argument("--output-root", action="append", required=True, help="Output generated test root.")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="Package name for copied Java files.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output roots.")
    parser.add_argument("--summary", help="Optional JSON summary path.")
    args = parser.parse_args()

    if len(args.source_root) != len(args.output_root):
        raise SystemExit("--source-root and --output-root must appear the same number of times")

    results = []
    for source_text, output_text in zip(args.source_root, args.output_root):
        result = prepare_root(
            source_root=Path(source_text),
            output_root=Path(output_text),
            package_name=args.package,
            force=args.force,
        )
        results.append(result)
        print(f"PREPARED {result['test_cases']:>3} {result['source_root']} -> {result['output_root']}")

    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SUMMARY  {args.summary}")


if __name__ == "__main__":
    main()
