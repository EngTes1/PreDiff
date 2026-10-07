import argparse
import json
import os
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from precondition_agent.corpus_batch import (
    CorpusUnit,
    discover_corpus_units,
    group_units,
    normalize_corpus_root,
    output_paths_for_unit,
    unit_to_dict,
)
from precondition_agent.profile import build_group_profile, build_unit_profile
from precondition_agent.schemas import AnalysisConfig
from precondition_agent.utils import load_text


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-reasoner"
DEFAULT_THINKING_MODE = "enabled"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_CORPUS_ROOT = r"E:\refactoring-preconditions-corpus"
DEFAULT_REPO_ROOTS = {
    "IntelliJ IDEA": r"E:\Open Source Repository\intellij-community-master",
    "Eclipse": r"E:\Open Source Repository\eclipse.jdt.ui",
    "netbeans": r"E:\Open Source Repository\netbeans-master",
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch analyze corpus snippets and emit strict precondition profiles."
    )
    parser.add_argument("--corpus-root", default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--refactoring", default="InlineMethod")
    parser.add_argument("--engine", default="", help="Optional corpus engine directory name.")
    parser.add_argument(
        "--implementation",
        default="",
        help="Optional implementation variant below the refactoring directory, e.g. legacy or newImpl.",
    )
    parser.add_argument(
        "--phase",
        action="append",
        default=[],
        help="Corpus phase to include, e.g. required. Repeat for multiple phases. Default: required.",
    )
    parser.add_argument("--max-files", type=int, default=0, help="Limit analyzed files for smoke runs.")
    parser.add_argument("--dry-run", action="store_true", help="Only list matched corpus units.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Load existing raw_analyses.json, skip successful units, and continue unfinished units.",
    )
    parser.add_argument("--no-strict", action="store_true", help="Do not drop low-support constraints.")
    parser.add_argument(
        "--output-root",
        default="",
        help="Optional output root for smoke runs. Default writes into each corpus engine/refactoring directory.",
    )

    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--thinking-mode", default=DEFAULT_THINKING_MODE)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--rg-path", default="rg")
    parser.add_argument("--java-path", default="")
    parser.add_argument("--javac-path", default="")
    parser.add_argument("--index-file", default=None)
    parser.add_argument("--disable-ast", action="store_true")
    parser.add_argument("--disable-repo-source-cache", action="store_true")
    parser.add_argument("--workspace-cache-dir", default=".cache")
    parser.add_argument(
        "--trace-level",
        choices=["none", "compact", "normal", "full"],
        default="compact",
        help="Trace verbosity stored in raw_analyses.json.",
    )
    parser.add_argument(
        "--repo-root",
        action="append",
        default=[],
        help="Override repo root as Engine=Path, e.g. Eclipse=E:\\repo\\eclipse.jdt.ui.",
    )
    return parser


def build_config(args: argparse.Namespace) -> AnalysisConfig:
    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        try:
            from main3 import DEBUG_API_KEY

            api_key = DEBUG_API_KEY
        except Exception:
            api_key = ""
    if not api_key:
        raise SystemExit("Missing API key. Set DEEPSEEK_API_KEY or pass --api-key.")

    return AnalysisConfig(
        api_key=api_key,
        base_url=args.base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        model_name=args.model or os.getenv("MODEL_NAME", DEFAULT_MODEL),
        thinking_mode=args.thinking_mode,
        reasoning_effort=args.reasoning_effort,
        temperature=args.temperature,
        repo_root="",
        rg_path=args.rg_path,
        index_file=args.index_file,
        java_path=args.java_path or os.getenv("JAVA_PATH", "java"),
        javac_path=args.javac_path or os.getenv("JAVAC_PATH", "javac"),
        enable_ast_retrieval=not args.disable_ast,
        enable_repo_source_cache=not args.disable_repo_source_cache,
        workspace_cache_dir=args.workspace_cache_dir,
        trace_level=args.trace_level,
    )


def repo_roots_from_args(raw_overrides: List[str]) -> Dict[str, str]:
    roots = dict(DEFAULT_REPO_ROOTS)
    for raw in raw_overrides:
        if "=" not in raw:
            raise SystemExit(f"Invalid --repo-root override, expected Engine=Path: {raw}")
        engine, path = raw.split("=", 1)
        roots[engine.strip()] = path.strip()
    return roots


def print_unit_start(index: int, total: int, unit: CorpusUnit) -> None:
    print(f"[{index:02d}/{total:02d}] {unit.engine}/{unit.refactoring}/{unit.phase}/{unit.path.name}", flush=True)


def print_step(name: str, status: str, duration: float, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    print(f"  {name:<10} {status:<7} {duration:7.2f}s{suffix}", flush=True)


def compact_raw_result(unit: CorpusUnit, result: Dict[str, Any]) -> Dict[str, Any]:
    analysis = result.get("analysis", {})
    probe_result = result.get("probe_result", {})
    return {
        "unit": unit_to_dict(unit),
        "engine": result.get("engine", unit.engine),
        "entry_name": result.get("entry_name", unit.entry_name),
        "snippet_language": result.get("snippet_language", ""),
        "probe_result": probe_result,
        "retrieved_contexts": result.get("retrieved_contexts", []),
        "grounded_symbols": result.get("grounded_symbols", []),
        "analysis": analysis,
        "trace": result.get("trace", []),
    }


def write_group_outputs(
    unit: CorpusUnit,
    repo_root: str,
    corpus_root: Path,
    output_root: Optional[Path],
    unit_profiles: List[Dict[str, Any]],
    raw_results: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    strict: bool,
) -> None:
    paths = output_paths_for_unit(unit, output_root=output_root)
    group_profile = build_group_profile(
        engine=unit.engine,
        refactoring=unit.refactoring,
        repo_root=repo_root,
        corpus_root=str(corpus_root),
        unit_profiles=unit_profiles,
        errors=errors,
        strict=strict,
    )
    paths.profile_path.write_text(json.dumps(group_profile, ensure_ascii=False, indent=2), encoding="utf-8")
    paths.raw_path.write_text(json.dumps(raw_results, ensure_ascii=False, indent=2), encoding="utf-8")
    paths.error_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")


def load_resume_state(
    unit: CorpusUnit,
    output_root: Optional[Path],
    strict: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], set]:
    paths = output_paths_for_unit(unit, output_root=output_root)
    raw_results = _read_json_list(paths.raw_path)
    errors = _read_json_list(paths.error_path)
    unit_profiles: List[Dict[str, Any]] = []
    completed_unit_ids = set()

    for raw in raw_results:
        if not isinstance(raw, dict) or not isinstance(raw.get("unit"), dict):
            continue
        unit_payload = raw["unit"]
        unit_id = str(unit_payload.get("unit_id", "")).strip()
        if not unit_id:
            continue
        try:
            unit_profiles.append(build_unit_profile(unit_payload, raw, strict=strict))
            completed_unit_ids.add(unit_id)
        except Exception:
            continue

    return unit_profiles, raw_results, errors, completed_unit_ids


def _read_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _remove_unit_errors(errors: List[Dict[str, Any]], unit_id: str) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        error_unit = error.get("unit", {})
        if isinstance(error_unit, dict) and error_unit.get("unit_id") == unit_id:
            continue
        filtered.append(error)
    return filtered


def analyze_units(args: argparse.Namespace) -> int:
    corpus_root = normalize_corpus_root(Path(args.corpus_root))
    phases = args.phase or ["required"]
    strict = not args.no_strict
    output_root = Path(args.output_root) if args.output_root else None
    units = discover_corpus_units(
        corpus_root=corpus_root,
        refactoring=args.refactoring,
        engine=args.engine,
        phases=phases,
        implementation=args.implementation,
    )
    if args.max_files > 0:
        units = units[: args.max_files]

    if not units:
        print("No corpus units matched.")
        return 1

    if args.dry_run:
        for unit in units:
            print(f"{unit.engine}\t{unit.refactoring}\t{unit.phase}\t{unit.path}")
        return 0

    repo_roots = repo_roots_from_args(args.repo_root)
    config = build_config(args)
    from precondition_agent.agent import PreconditionAnalysisAgent

    agent = PreconditionAnalysisAgent(config)
    grouped_profiles: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    grouped_raw: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    grouped_errors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    completed_by_group: Dict[Tuple[str, str], set] = {}
    resume_loaded_groups = set()

    try:
        for index, unit in enumerate(units, start=1):
            key = (unit.engine, unit.refactoring)
            grouped_profiles.setdefault(key, [])
            grouped_raw.setdefault(key, [])
            grouped_errors.setdefault(key, [])
            completed_by_group.setdefault(key, set())

            if args.resume and key not in resume_loaded_groups:
                profiles, raw_results, errors, completed = load_resume_state(
                    unit=unit,
                    output_root=output_root,
                    strict=strict,
                )
                grouped_profiles[key] = profiles
                grouped_raw[key] = raw_results
                grouped_errors[key] = errors
                completed_by_group[key] = completed
                resume_loaded_groups.add(key)

            if args.resume and unit.unit_id in completed_by_group[key]:
                print_unit_start(index, len(units), unit)
                print_step("RESUME", "skip", 0.0, "already in raw_analyses.json")
                continue

            print_unit_start(index, len(units), unit)
            repo_root = repo_roots.get(unit.engine, "")
            if not repo_root:
                error = _error_payload(unit, "missing_repo_root", f"No repo root configured for {unit.engine}")
                grouped_errors[key].append(error)
                print_step("CONFIG", "failed", 0.0, error["message"])
                continue

            started = time.time()
            try:
                snippet = load_text(str(unit.path)).strip()
                print_step("READ", "ok", time.time() - started, f"chars={len(snippet)}")

                started = time.time()
                result = agent.analyze_one(
                    engine=unit.engine,
                    repo_root=repo_root,
                    snippet=snippet,
                    entry_name=unit.entry_name,
                    index_file=args.index_file,
                )
                analysis = result.get("analysis", {})
                print_step(
                    "ANALYZE",
                    "ok",
                    time.time() - started,
                    (
                        f"constraints={len(analysis.get('constraints', []))} "
                        f"contexts={len(result.get('retrieved_contexts', []))}"
                    ),
                )

                started = time.time()
                unit_profile = build_unit_profile(unit_to_dict(unit), result, strict=strict)
                grouped_errors[key] = _remove_unit_errors(grouped_errors[key], unit.unit_id)
                grouped_profiles[key].append(unit_profile)
                grouped_raw[key].append(compact_raw_result(unit, result))
                completed_by_group[key].add(unit.unit_id)
                print_step(
                    "VALIDATE",
                    "ok",
                    time.time() - started,
                    (
                        f"kept={len(unit_profile['constraints'])} "
                        f"uncertain={len(unit_profile['uncertain_constraints'])} "
                        f"dropped={len(unit_profile['dropped_claims'])}"
                    ),
                )
            except Exception as exc:
                grouped_errors[key] = _remove_unit_errors(grouped_errors[key], unit.unit_id)
                error = _error_payload(unit, type(exc).__name__, str(exc))
                grouped_errors[key].append(error)
                print_step("ERROR", "failed", 0.0, error["message"][:180])

            started = time.time()
            write_group_outputs(
                unit=unit,
                repo_root=repo_root,
                corpus_root=corpus_root,
                output_root=output_root,
                unit_profiles=grouped_profiles[key],
                raw_results=grouped_raw[key],
                errors=grouped_errors[key],
                strict=strict,
            )
            paths = output_paths_for_unit(unit, output_root=output_root)
            print_step("WRITE", "ok", time.time() - started, str(paths.profile_path))
    finally:
        agent.close()

    print("Batch summary:")
    for (engine, refactoring), profiles in sorted(grouped_profiles.items()):
        errors = grouped_errors.get((engine, refactoring), [])
        kept = sum(len(item.get("constraints", [])) for item in profiles)
        dropped = sum(len(item.get("dropped_claims", [])) for item in profiles)
        print(
            f"  {engine}/{refactoring}: units={len(profiles)} constraints={kept} "
            f"dropped={dropped} errors={len(errors)}"
        )
    return 0


def _error_payload(unit: CorpusUnit, error_type: str, message: str) -> Dict[str, Any]:
    return {
        "unit": unit_to_dict(unit),
        "error_type": error_type,
        "message": message,
    }


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    raise SystemExit(analyze_units(args))


if __name__ == "__main__":
    main()
