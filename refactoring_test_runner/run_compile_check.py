import argparse
import json
import os
import sys
import time

from pathlib import Path
from typing import Any, Dict, List

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refactoring_test_runner.compile_run import compile_and_run_case
from refactoring_test_runner.discovery import discover_test_cases


SCHEMA_VERSION = "refactoring_compile_run_results_v1"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile and run generated Java refactoring test cases."
    )
    parser.add_argument("--test-root", required=True, help="Generated test root or test_cases.json path.")
    parser.add_argument("--include", default="*", help="fnmatch pattern for test ids, e.g. InlineMethod_S001_*.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--java-path", default=os.getenv("JAVA_PATH", "java"))
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--compile-timeout", type=int, default=10)
    parser.add_argument("--run-timeout", type=int, default=5)
    parser.add_argument("--no-clean", action="store_true", help="Do not remove each case out/ directory first.")
    return parser


def build_summary(results: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        "total": len(results),
        "compile_ok": 0,
        "compile_failed": 0,
        "compile_timeout": 0,
        "run_ok": 0,
        "run_failed": 0,
        "run_timeout": 0,
        "run_not_run": 0,
    }
    for item in results:
        compile_status = str((item.get("compile", {}) or {}).get("status", ""))
        run_status = str((item.get("run", {}) or {}).get("status", ""))
        if compile_status == "ok":
            summary["compile_ok"] += 1
        elif compile_status == "timeout":
            summary["compile_timeout"] += 1
        else:
            summary["compile_failed"] += 1

        if run_status == "ok":
            summary["run_ok"] += 1
        elif run_status == "timeout":
            summary["run_timeout"] += 1
        elif run_status == "not_run":
            summary["run_not_run"] += 1
        else:
            summary["run_failed"] += 1
    return summary


def default_output_path(test_root: str) -> Path:
    name = Path(test_root).stem or "compile_run"
    return Path(".cache") / "test_runner" / f"{name}_compile_run.json"


def main() -> None:
    args = build_arg_parser().parse_args()
    started_at = time.time()
    cases = discover_test_cases(args.test_root, include=args.include)
    print(f"Discovered test cases: {len(cases)}", flush=True)
    results: List[Dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index:03d}/{len(cases):03d}] {case.test_id}", flush=True)
        result = compile_and_run_case(
            case,
            java_path=args.java_path,
            javac_path=args.javac_path,
            compile_timeout=args.compile_timeout,
            run_timeout=args.run_timeout,
            clean=not args.no_clean,
        )
        compile_status = result["compile"]["status"]
        run_status = result["run"]["status"]
        print(f"  COMPILE {compile_status:<8} RUN {run_status:<8}", flush=True)
        results.append(result)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "test_root": str(Path(args.test_root)),
        "include": args.include,
        "java_path": args.java_path,
        "javac_path": args.javac_path,
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
