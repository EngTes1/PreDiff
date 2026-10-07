import argparse
import json
import os
import time

from pathlib import Path
from typing import List

from precondition_agent.schemas import AnalysisConfig
from precondition_agent.test_generation import (
    TestGenerationAgent,
    load_comparison_report,
    materialize_test_suite,
    normalize_test_suite,
    safe_refactoring_name,
    select_generation_scenarios,
)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_THINKING_MODE = "enabled"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_OUTPUT_ROOT = "generated_refactoring_tests"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Java refactoring test cases from a comparison report."
    )
    parser.add_argument("--comparison", required=True, help="Path to precondition comparison report JSON.")
    parser.add_argument("--output-root", default="", help="Default: generated_refactoring_tests/<Refactoring>.")
    parser.add_argument("--max-scenarios", type=int, default=3)
    parser.add_argument("--tests-per-scenario", type=int, default=10)
    parser.add_argument(
        "--scenario-id",
        action="append",
        default=[],
        help="Generate only the given scenario id, e.g. S006. Repeat for multiple scenarios.",
    )
    parser.add_argument(
        "--layout",
        choices=["simple", "maven"],
        default="simple",
        help="Output layout. simple writes src/<Class>.java; maven also writes pom.xml and src/main/java.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only select scenarios and write a generation plan; do not call the LLM.",
    )

    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--thinking-mode", default=DEFAULT_THINKING_MODE)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--extra-guidance-file",
        default="",
        help="Optional UTF-8 text file appended to the built-in generation prompt.",
    )
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


def resolve_output_root(args: argparse.Namespace, refactoring: str) -> Path:
    if args.output_root:
        return Path(args.output_root)
    return Path(DEFAULT_OUTPUT_ROOT) / safe_refactoring_name(refactoring)


def main() -> None:
    args = build_arg_parser().parse_args()
    started_at = time.time()
    report = load_comparison_report(args.comparison)
    refactoring = str(report.get("refactoring", "")).strip() or "Refactoring"
    output_root = resolve_output_root(args, refactoring)

    scenarios = select_generation_scenarios(
        report,
        max_scenarios=args.max_scenarios,
        scenario_ids=args.scenario_id,
    )
    print(
        f"Generation input: refactoring={refactoring} scenarios={len(scenarios)} "
        f"tests_per_scenario={args.tests_per_scenario}",
        flush=True,
    )

    if args.dry_run:
        plan = {
            "status": "dry_run",
            "refactoring": refactoring,
            "source_comparison_report": args.comparison,
            "max_scenarios": args.max_scenarios,
            "scenario_ids": args.scenario_id,
            "tests_per_scenario": args.tests_per_scenario,
            "layout": args.layout,
            "selected_scenarios": scenarios,
        }
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "generation_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Dry-run plan written: {output_root / 'generation_plan.json'}", flush=True)
        return

    config = build_config(args)
    agent = TestGenerationAgent(config)
    extra_guidance = ""
    if args.extra_guidance_file:
        extra_guidance = Path(args.extra_guidance_file).read_text(encoding="utf-8")
    print("GENERATE  running LLM test generation", flush=True)
    suite = agent.generate_tests(
        report,
        max_scenarios=args.max_scenarios,
        tests_per_scenario=args.tests_per_scenario,
        source_report=args.comparison,
        layout=args.layout,
        scenario_ids=args.scenario_id,
        extra_guidance=extra_guidance,
    )
    written = materialize_test_suite(suite, output_root)
    print(f"WRITE     ok {time.time() - started_at:.2f}s {output_root}", flush=True)
    print(f"FILES     {len(written)} files written, including test_cases.json", flush=True)


if __name__ == "__main__":
    main()
