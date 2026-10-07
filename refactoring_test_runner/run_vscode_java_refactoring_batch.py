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
from refactoring_test_runner.run_eclipse_refactoring import build_summary
from refactoring_test_runner.vscode_java_runner import (
    VscodeJavaJdtlsBackend,
    VscodeJavaRefactoringRunner,
    find_vscode_java_extension,
)


SCHEMA_VERSION = "vscode_java_refactoring_batch_results_v1"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch-run generated Java tests through VS Code Java/JDT LS refactoring code actions."
    )
    parser.add_argument(
        "--test-root",
        action="append",
        required=True,
        help="Generated test root, test_cases.json, or glob. Repeat for multiple roots.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="fnmatch pattern for test ids. Repeat to include multiple patterns.",
    )
    parser.add_argument("--output", default=".cache/test_runner/vscode_java_batch_results.json")
    parser.add_argument("--workspace-root", default=".cache/test_runner/vscode_java_work")
    parser.add_argument("--jdtls-data-root", default=".cache/test_runner/vscode_java_jdtls_workspaces")
    parser.add_argument(
        "--vscode-java-extension",
        default=os.getenv("VSCODE_JAVA_EXTENSION", ""),
        help="Path to the redhat.java VS Code extension directory. Auto-detected when omitted.",
    )
    parser.add_argument("--java-path", default=os.getenv("JAVA_PATH", "java"))
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--compile-timeout", type=int, default=10)
    parser.add_argument("--run-timeout", type=int, default=5)
    parser.add_argument("--refactor-timeout", type=int, default=180)
    parser.add_argument("--startup-timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of discovered cases to run.")
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
    extension_dir = Path(args.vscode_java_extension).resolve() if args.vscode_java_extension else find_vscode_java_extension()
    if extension_dir is None:
        raise FileNotFoundError(
            "Could not find redhat.java VS Code extension. Pass --vscode-java-extension explicitly."
        )

    backend = VscodeJavaJdtlsBackend(
        extension_dir=extension_dir,
        data_root=args.jdtls_data_root,
        java_path=args.java_path,
        timeout=args.refactor_timeout,
        startup_timeout=args.startup_timeout,
    )
    runner = VscodeJavaRefactoringRunner(
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
            root_cases = discover_test_cases(root, include=include)
            for case in root_cases:
                if case.test_id in seen_case_ids:
                    continue
                seen_case_ids.add(case.test_id)
                cases.append((root, case))
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    print(f"Discovered roots: {len(roots)}", flush=True)
    print(f"Discovered test cases: {len(cases)}", flush=True)
    print(f"VSCODE_JAVA_EXTENSION {extension_dir}", flush=True)

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
        "engine": "VS Code Java",
        "test_roots": [str(path) for path in roots],
        "include": include_patterns,
        "workspace_root": str(Path(args.workspace_root)),
        "jdtls_data_root": str(Path(args.jdtls_data_root)),
        "vscode_java_extension": str(extension_dir),
        "java_path": args.java_path,
        "javac_path": args.javac_path,
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
