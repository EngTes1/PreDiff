import argparse
import json
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from precondition_agent.agent import PreconditionAnalysisAgent
from precondition_agent.schemas import AnalysisConfig


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prebuild repository source-name indexes.")
    parser.add_argument("--workspace-cache-dir", required=True)
    parser.add_argument("--rg-path", default="rg")
    parser.add_argument("--repo-root", action="append", default=[], required=True)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    agent = PreconditionAnalysisAgent(
        AnalysisConfig(
            api_key="cache-preparation-only",
            workspace_cache_dir=args.workspace_cache_dir,
            rg_path=args.rg_path,
            enable_repo_source_cache=True,
            enable_ast_retrieval=True,
        )
    )

    for repo_root in args.repo_root:
        index = agent._load_repo_source_name_index(repo_root)
        files = sum(len(paths) for paths in index.values())
        print(f"INDEXED {repo_root}: names={len(index)} files={files}", flush=True)

    index_dir = Path(args.workspace_cache_dir) / "repo_source_indexes"
    cache_files = sorted(index_dir.glob("*.json"))
    if len(cache_files) != len(args.repo_root):
        raise SystemExit(
            f"Expected {len(args.repo_root)} repository index files, "
            f"found {len(cache_files)} in {index_dir}"
        )

    for cache_file in cache_files:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(payload.get("index"), dict) or not payload["index"]:
            raise SystemExit(f"Invalid or empty repository index: {cache_file}")
        print(f"VERIFIED {cache_file}", flush=True)


if __name__ == "__main__":
    main()
