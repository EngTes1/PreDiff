import argparse
import json
import os

from typing import Any, Dict

from precondition_agent.agent import PreconditionAnalysisAgent
from precondition_agent.schemas import AnalysisConfig
from precondition_agent.utils import infer_entry_name, load_text


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-reasoner"
DEFAULT_THINKING_MODE = "enabled"
DEFAULT_REASONING_EFFORT = "high"

# =========================
# Debug Input For Local Tuning
DEBUG_API_KEY = ""
DEBUG_ENGINE = "Eclipse"
DEBUG_REPO_ROOT = r"E:\Open Source Repository\eclipse.jdt.ui"
DEBUG_INDEX_FILE = "file_index.json"
DEBUG_ENTRY_NAME = ""
DEBUG_SNIPPET = r"""
public RefactoringStatus checkInitialConditions(IProgressMonitor pm) throws CoreException {
		if (! fMethod.exists()){
			String message= Messages.format(RefactoringCoreMessages.RenameMethodRefactoring_deleted,
								BasicElementLabels.getFileName(fMethod.getCompilationUnit()));
			return RefactoringStatus.createFatalErrorStatus(message);
		}

		RefactoringStatus result= Checks.checkAvailability(fMethod);
		if (result.hasFatalError())
				return result;
		result.merge(Checks.checkIfCuBroken(fMethod));
		if (JdtFlags.isNative(fMethod))
			result.addError(RefactoringCoreMessages.RenameMethodRefactoring_no_native);
		return result;
	}
""".strip()


def build_config_from_env(args: argparse.Namespace) -> AnalysisConfig:
    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "") or DEBUG_API_KEY
    if not api_key:
        raise SystemExit("Missing API key. Set DEEPSEEK_API_KEY or pass --api-key.")

    trace_stages = []
    for raw_stage in args.trace_stage or []:
        trace_stages.extend([stage.strip() for stage in raw_stage.split(",") if stage.strip()])

    return AnalysisConfig(
        api_key=api_key,
        base_url=args.base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        model_name=args.model or os.getenv("MODEL_NAME", DEFAULT_MODEL),
        thinking_mode=args.thinking_mode or os.getenv("DEEPSEEK_THINKING_MODE", DEFAULT_THINKING_MODE),
        reasoning_effort=args.reasoning_effort or os.getenv("DEEPSEEK_REASONING_EFFORT", DEFAULT_REASONING_EFFORT),
        temperature=args.temperature,
        repo_root=args.repo_root or "",
        rg_path=args.rg_path or os.getenv("RG_PATH", "rg"),
        index_file=args.index_file,
        java_path=args.java_path or os.getenv("JAVA_PATH", "java"),
        javac_path=args.javac_path or os.getenv("JAVAC_PATH", "javac"),
        enable_ast_retrieval=not args.disable_ast,
        trace_level=args.trace_level,
        trace_stages=trace_stages or None,
        max_candidate_paths_per_symbol=args.max_candidate_paths_per_symbol,
        max_ast_candidates_for_planning=args.max_ast_candidates_for_planning,
        enable_repo_source_cache=not args.disable_repo_source_cache,
        workspace_cache_dir=args.workspace_cache_dir,
    )


def run_single(agent: PreconditionAnalysisAgent, args: argparse.Namespace) -> Dict[str, Any]:
    snippet = args.snippet_text or (load_text(args.snippet_file) if args.snippet_file else DEBUG_SNIPPET)
    entry_name = args.entry_name or DEBUG_ENTRY_NAME or infer_entry_name(snippet)
    return agent.analyze_one(
        engine=args.engine or DEBUG_ENGINE,
        repo_root=args.repo_root or DEBUG_REPO_ROOT,
        snippet=snippet,
        entry_name=entry_name,
        index_file=args.index_file or DEBUG_INDEX_FILE,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Step-2 pipeline for refactoring precondition analysis."
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--thinking-mode",
        choices=["", "enabled", "disabled"],
        default=DEFAULT_THINKING_MODE,
        help="DeepSeek thinking mode. Use 'enabled' for supported reasoning models.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help="DeepSeek reasoning effort, for example low, medium, high, or max when supported.",
    )
    parser.add_argument("--temperature", type=float, default=0.0)

    parser.add_argument("--engine", default="IntelliJ")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--index-file", default=None)
    parser.add_argument("--rg-path", default="rg")
    parser.add_argument("--java-path", default="")
    parser.add_argument("--javac-path", default="")
    parser.add_argument("--disable-ast", action="store_true")
    parser.add_argument("--max-candidate-paths-per-symbol", type=int, default=24)
    parser.add_argument("--max-ast-candidates-for-planning", type=int, default=10)
    parser.add_argument("--disable-repo-source-cache", action="store_true")
    parser.add_argument("--workspace-cache-dir", default=".cache")
    parser.add_argument(
        "--trace-level",
        choices=["none", "compact", "normal", "full"],
        default="normal",
        help="Control trace verbosity in output JSON.",
    )
    parser.add_argument(
        "--trace-stage",
        action="append",
        default=[],
        help="Only include this trace stage. Repeat to include multiple stages.",
    )
    parser.add_argument("--entry-name", default="")

    parser.add_argument("--snippet-file", default="")
    parser.add_argument("--snippet-text", default="")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.engine:
        args.engine = DEBUG_ENGINE
    if not args.repo_root:
        args.repo_root = DEBUG_REPO_ROOT
    if not args.index_file:
        args.index_file = DEBUG_INDEX_FILE
    if not args.entry_name and DEBUG_ENTRY_NAME:
        args.entry_name = DEBUG_ENTRY_NAME

    config = build_config_from_env(args)
    agent = PreconditionAnalysisAgent(config)
   
    try:
        if not args.repo_root:
            raise SystemExit("Single-run mode requires --repo-root or DEBUG_REPO_ROOT.")
        if not args.snippet_file and not args.snippet_text and not DEBUG_SNIPPET:
            raise SystemExit("Provide --snippet-file / --snippet-text, or fill DEBUG_SNIPPET in code.")
        result = run_single(agent, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        agent.close()


if __name__ == "__main__":
    main()
