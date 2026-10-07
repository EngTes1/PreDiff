from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AnalysisConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    thinking_mode: str = ""
    reasoning_effort: str = ""
    temperature: float = 0.0
    repo_root: str = ""
    rg_path: str = "rg"
    index_file: Optional[str] = None
    java_path: str = "java"
    javac_path: str = "javac"
    enable_ast_retrieval: bool = True
    max_symbol_candidates: int = 12
    max_search_hits_per_query: int = 12
    max_contexts_per_symbol: int = 2
    max_total_contexts: int = 12
    max_candidate_paths_per_symbol: int = 24
    max_ast_candidates_for_planning: int = 10
    enable_repo_source_cache: bool = True
    workspace_cache_dir: str = ".cache"
    trace_level: str = "normal"
    trace_stages: Optional[List[str]] = None


@dataclass
class TraceEvent:
    stage: str
    payload: Dict[str, Any]
    duration_ms: int


@dataclass
class RetrievalHit:
    path: str
    line: int
    text: str
    source: str = "rg"
    score: float = 0.0
    preview: str = ""
    end_line: Optional[int] = None
    node_kind: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievedContext:
    symbol: str
    kind: str
    query: str
    path: str
    anchor_line: int
    start_line: int
    end_line: int
    reason: str
    source: str
    code: str


@dataclass
class GroundedSymbol:
    symbol: str
    kind: str
    grounded_meaning: str
    role_in_snippet: str
    evidence: List[str] = field(default_factory=list)
    confidence: str = "medium"


@dataclass
class EngineInput:
    engine: str
    repo_root: str
    snippet: str
    entry_name: str = ""
    index_file: Optional[str] = None
