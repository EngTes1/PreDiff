import argparse
import json
import os
import sys
import time

from pathlib import Path
from typing import Any, Dict, List

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refactoring_test_runner.discovery import discover_test_cases
from refactoring_test_runner.eclipse_runner import (
    CommandEclipseBackend,
    EclipseApplicationBackend,
    EclipseRefactoringRunner,
    UnsupportedEclipseBackend,
)


SCHEMA_VERSION = "eclipse_refactoring_run_results_v1"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run generated Java tests through an Eclipse refactoring backend, then compile/run before and after."
    )
    parser.add_argument("--test-root", required=True, help="Generated test root or test_cases.json path.")
    parser.add_argument("--include", default="*", help="fnmatch pattern for test ids, e.g. InlineMethod_S001_*.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--workspace-root", default=".cache/test_runner/eclipse_work")
    parser.add_argument("--java-path", default=os.getenv("JAVA_PATH", "java"))
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--compile-timeout", type=int, default=10)
    parser.add_argument("--run-timeout", type=int, default=5)
    parser.add_argument("--refactor-timeout", type=int, default=60)
    parser.add_argument("--eclipse-home", default="", help="Eclipse installation directory containing eclipsec.exe.")
    parser.add_argument("--eclipse-config", default=".cache/eclipse_backend/configuration")
    parser.add_argument("--eclipse-data-root", default=".cache/test_runner/eclipse_backend_workspaces")
    parser.add_argument("--eclipse-vm", default=os.getenv("ECLIPSE_VM", ""))
    parser.add_argument(
        "--eclipse-application",
        default="experiment.eclipse.refactoring.backend.application",
        help="Headless Eclipse application id for the refactoring backend.",
    )
    parser.add_argument(
        "--eclipse-command",
        default="",
        help=(
            "Optional external Eclipse/JDT refactoring backend executable. "
            "It will receive --case-dir, --java-file, --refactoring, --target-method, and --output."
        ),
    )
    parser.add_argument(
        "--eclipse-command-arg",
        action="append",
        default=[],
        help="Extra argument prepended after --eclipse-command. Can be repeated.",
    )
    return parser


def make_backend(args: argparse.Namespace) -> UnsupportedEclipseBackend | CommandEclipseBackend:
    if not args.eclipse_command:
        if args.eclipse_home:
            return EclipseApplicationBackend(
                eclipse_home=args.eclipse_home,
                configuration=args.eclipse_config,
                data_root=args.eclipse_data_root,
                application_id=args.eclipse_application,
                vm_path=args.eclipse_vm,
                timeout=args.refactor_timeout,
            )
        return UnsupportedEclipseBackend()
    return CommandEclipseBackend(
        [args.eclipse_command] + list(args.eclipse_command_arg or []),
        timeout=args.refactor_timeout,
    )


def build_summary(results: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        "total": len(results),
        "before_compile_ok": 0,
        "before_run_ok": 0,
        "refactoring_success": 0,
        "refactoring_warning": 0,
        "refactoring_rejected": 0,
        "refactoring_failed": 0,
        "refactoring_unsupported": 0,
        "refactoring_timeout": 0,
        "refactoring_skipped": 0,
        "applied": 0,
        "after_compile_ok": 0,
        "after_run_ok": 0,
        "behavior_preserved_true": 0,
        "behavior_preserved_false": 0,
        "behavior_preserved_unknown": 0,
    }
    for item in results:
        before = item.get("before", {}) or {}
        after = item.get("after", {}) or {}
        refactoring = item.get("refactoring", {}) or {}

        if (before.get("compile", {}) or {}).get("status") == "ok":
            summary["before_compile_ok"] += 1
        if (before.get("run", {}) or {}).get("status") == "ok":
            summary["before_run_ok"] += 1

        status = str(refactoring.get("status") or "failed")
        key = f"refactoring_{status}"
        if key in summary:
            summary[key] += 1
        else:
            summary["refactoring_failed"] += 1
        if refactoring.get("applied"):
            summary["applied"] += 1

        if (after.get("compile", {}) or {}).get("status") == "ok":
            summary["after_compile_ok"] += 1
        if (after.get("run", {}) or {}).get("status") == "ok":
            summary["after_run_ok"] += 1

        preserved = item.get("behavior_preserved")
        if preserved is True:
            summary["behavior_preserved_true"] += 1
        elif preserved is False:
            summary["behavior_preserved_false"] += 1
        else:
            summary["behavior_preserved_unknown"] += 1
    return summary


def default_output_path(test_root: str) -> Path:
    name = Path(test_root).stem or "eclipse_refactoring"
    return Path(".cache") / "test_runner" / f"{name}_eclipse_refactoring.json"


def main() -> None:
    args = build_arg_parser().parse_args()
    started_at = time.time()
    cases = discover_test_cases(args.test_root, include=args.include)
    backend = make_backend(args)
    runner = EclipseRefactoringRunner(
        backend=backend,
        java_path=args.java_path,
        javac_path=args.javac_path,
        compile_timeout=args.compile_timeout,
        run_timeout=args.run_timeout,
    )

    print(f"Discovered test cases: {len(cases)}", flush=True)
    print(f"ECLIPSE_BACKEND {getattr(backend, 'name', backend.__class__.__name__)}", flush=True)

    results: List[Dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index:03d}/{len(cases):03d}] {case.test_id}", flush=True)
        result = runner.run_case(case, args.workspace_root)
        refactoring = result["refactoring"]
        before = result["before"]
        after = result["after"]
        print(
            f"  BEFORE compile={before['compile']['status']} run={before['run']['status']} "
            f"REF={refactoring['status']} applied={str(refactoring['applied']).lower()} "
            f"AFTER={after.get('compile', {}).get('status', after.get('status'))}/"
            f"{after.get('run', {}).get('status', after.get('status'))} "
            f"BEHAVIOR={result['behavior_preserved']}",
            flush=True,
        )
        results.append(result)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "engine": "Eclipse",
        "test_root": str(Path(args.test_root)),
        "include": args.include,
        "workspace_root": str(Path(args.workspace_root)),
        "java_path": args.java_path,
        "javac_path": args.javac_path,
        "backend": getattr(backend, "name", backend.__class__.__name__),
        "summary": build_summary(results),
        "results": results,
        "duration_ms": int((time.time() - started_at) * 1000),
    }
    output_path = Path(args.output) if args.output else default_output_path(args.test_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WRITE {output_path}", flush=True)


if __name__ == "__main__":
    main()
