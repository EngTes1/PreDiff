import argparse
import glob
import json
import os
import sys
import time

from pathlib import Path
from typing import Any, Dict, List

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refactoring_test_runner.discovery import discover_test_cases
from refactoring_test_runner.intellij_runner import IntelliJApplicationBackend, IntelliJRefactoringRunner
from refactoring_test_runner.run_eclipse_refactoring import build_summary


SCHEMA_VERSION = "intellij_refactoring_batch_results_v1"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch-run generated Java tests through the IntelliJ IDEA refactoring backend.")
    parser.add_argument("--test-root", action="append", required=True)
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="fnmatch pattern for test ids. Repeat to include multiple patterns.",
    )
    parser.add_argument("--output", default=".cache/test_runner/intellij_batch_results.json")
    parser.add_argument("--workspace-root", default=".cache/test_runner/intellij_work")
    parser.add_argument("--intellij-home", required=True)
    parser.add_argument("--plugin-root", default=".cache/intellij_backend/plugins")
    parser.add_argument("--intellij-data-root", default=".cache/test_runner/intellij_backend_workspaces")
    parser.add_argument("--java-path", default=os.getenv("JAVA_PATH", "java"))
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--compile-timeout", type=int, default=10)
    parser.add_argument("--run-timeout", type=int, default=5)
    parser.add_argument("--refactor-timeout", type=int, default=240)
    parser.add_argument("--limit", type=int, default=0)
    return parser


def collect_test_roots(patterns: List[str]) -> List[Path]:
    roots: List[Path] = []
    seen = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            matches = [pattern]
        for match in matches:
            path = Path(match).resolve()
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
    return sorted(roots, key=lambda item: str(item).lower())


def main() -> None:
    args = build_arg_parser().parse_args()
    started_at = time.time()
    roots = collect_test_roots(args.test_root)
    backend = IntelliJApplicationBackend(
        intellij_home=args.intellij_home,
        plugin_root=args.plugin_root,
        data_root=args.intellij_data_root,
        timeout=args.refactor_timeout,
    )
    runner = IntelliJRefactoringRunner(
        backend=backend,
        java_path=args.java_path,
        javac_path=args.javac_path,
        compile_timeout=args.compile_timeout,
        run_timeout=args.run_timeout,
    )

    include_patterns = args.include or ["*"]
    cases = []
    for root in roots:
        seen_case_ids = set()
        for include in include_patterns:
            for case in discover_test_cases(root, include=include):
                if case.test_id in seen_case_ids:
                    continue
                seen_case_ids.add(case.test_id)
                cases.append((root, case))
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    print(f"Discovered roots: {len(roots)}", flush=True)
    print(f"Discovered test cases: {len(cases)}", flush=True)

    results: List[Dict[str, Any]] = []
    root_summaries: Dict[str, Dict[str, int]] = {}
    for index, (root, case) in enumerate(cases, start=1):
        print(f"[{index:03d}/{len(cases):03d}] {root.name}/{case.test_id}", flush=True)
        case_workspace_root = Path(args.workspace_root) / root.name
        result = runner.run_case(case, case_workspace_root)
        result["test_root"] = str(root)
        before = result["before"]
        refactoring = result["refactoring"]
        after = result["after"]
        print(
            f"  BEFORE compile={before['compile']['status']} run={before['run']['status']} "
            f"REF={refactoring['status']} severity={refactoring['severity']} applied={str(refactoring['applied']).lower()} "
            f"AFTER={after.get('compile', {}).get('status', after.get('status'))}/"
            f"{after.get('run', {}).get('status', after.get('status'))} "
            f"BEHAVIOR={result['behavior_preserved']}",
            flush=True,
        )
        results.append(result)
        root_key = str(root)
        root_results = [item for item in results if item.get("test_root") == root_key]
        root_summaries[root_key] = build_summary(root_results)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "engine": "IntelliJ IDEA",
        "test_roots": [str(path) for path in roots],
        "include": include_patterns,
        "summary": build_summary(results),
        "root_summaries": root_summaries,
        "results": results,
        "duration_ms": int((time.time() - started_at) * 1000),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WRITE {output}", flush=True)


if __name__ == "__main__":
    main()
