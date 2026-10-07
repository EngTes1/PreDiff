import argparse
import json
import os
import time

from pathlib import Path
from typing import Any, Dict, List

from precondition_agent.comparison import (
    ConsistencyComparisonAgent,
    discover_profile_paths,
    infer_refactoring_name,
    load_profile,
    summarize_profile,
)
from precondition_agent.schemas import AnalysisConfig


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_THINKING_MODE = "enabled"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_CORPUS_ROOT = r"E:\refactoring-preconditions-corpus"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare precondition profiles across refactoring engines."
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="Path to a precondition_profile.json. Repeat for multiple engines.",
    )
    parser.add_argument(
        "--corpus-root",
        default=DEFAULT_CORPUS_ROOT,
        help="Corpus root used when --profile is not supplied.",
    )
    parser.add_argument(
        "--refactoring",
        default="",
        help="Refactoring to compare, e.g. RenameMethod, InlineMethod, MoveInstanceMethod.",
    )
    parser.add_argument(
        "--engine",
        action="append",
        default=[],
        help="Optional engine filter when discovering profiles. Repeat for multiple engines.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Default: .cache/comparison/<refactoring>_comparison_report.json",
    )
    parser.add_argument(
        "--max-constraints-per-engine",
        type=int,
        default=160,
        help="Prompt safety limit per engine profile.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only load and summarize profiles; do not call the LLM.",
    )

    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--thinking-mode", default=DEFAULT_THINKING_MODE)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--trace-level",
        choices=["none", "compact", "normal", "full"],
        default="compact",
    )
    parser.add_argument(
        "--trace-stage",
        action="append",
        default=[],
        help="Optional trace stage filter. Repeat or pass comma-separated stages.",
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
    if not api_key and not args.dry_run:
        raise SystemExit("Missing API key. Set DEEPSEEK_API_KEY or pass --api-key.")

    trace_stages: List[str] = []
    for raw_stage in args.trace_stage or []:
        trace_stages.extend([stage.strip() for stage in raw_stage.split(",") if stage.strip()])

    return AnalysisConfig(
        api_key=api_key or "dry-run",
        base_url=args.base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        model_name=args.model or os.getenv("MODEL_NAME", DEFAULT_MODEL),
        thinking_mode=args.thinking_mode,
        reasoning_effort=args.reasoning_effort,
        temperature=args.temperature,
        trace_level=args.trace_level,
        trace_stages=trace_stages or None,
    )


def resolve_profile_paths(args: argparse.Namespace) -> List[Path]:
    if args.profile:
        return [Path(path) for path in args.profile]
    if not args.refactoring:
        raise SystemExit("Pass --refactoring when --profile is not supplied.")
    paths = discover_profile_paths(args.corpus_root, args.refactoring, engines=args.engine)
    if not paths:
        raise SystemExit(f"No precondition_profile.json matched refactoring={args.refactoring!r}.")
    return paths


def default_output_path(refactoring: str) -> Path:
    name = refactoring.replace("/", "_").replace("\\", "_") or "comparison"
    return Path(".cache") / "comparison" / f"{name}_comparison_report.json"


def print_profile_summary(profile: Dict[str, Any], path: Path) -> None:
    summary = profile.get("summary", {})
    print(
        f"  {profile.get('engine', '')}/{profile.get('refactoring', '')}: "
        f"constraints={summary.get('constraints', len(profile.get('constraints', []) or []))} "
        f"units={summary.get('units', len(profile.get('source_units', []) or []))} "
        f"path={path}",
        flush=True,
    )


def main() -> None:
    args = build_arg_parser().parse_args()
    started_at = time.time()
    profile_paths = resolve_profile_paths(args)
    profiles = [load_profile(path) for path in profile_paths]
    if len(profiles) < 2:
        raise SystemExit("Need at least two profiles to compare.")

    refactoring = args.refactoring or infer_refactoring_name([summarize_profile(profile) for profile in profiles])
    output_path = Path(args.output) if args.output else default_output_path(refactoring)

    print(f"Comparison input profiles: {len(profiles)}", flush=True)
    for profile, path in zip(profiles, profile_paths):
        print_profile_summary(profile, path)

    if args.dry_run:
        payload = {
            "status": "dry_run",
            "refactoring": refactoring,
            "profiles": [
                summarize_profile(profile, max_constraints=args.max_constraints_per_engine)
                for profile in profiles
            ],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Dry-run summary written: {output_path}", flush=True)
        return

    config = build_config(args)
    agent = ConsistencyComparisonAgent(config)
    print("COMPARE   running LLM consistency comparison", flush=True)
    report = agent.compare_profiles(
        profiles,
        refactoring=refactoring,
        max_constraints_per_engine=args.max_constraints_per_engine,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WRITE     ok {time.time() - started_at:.2f}s {output_path}", flush=True)


if __name__ == "__main__":
    main()
