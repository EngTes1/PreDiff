import json
import os
import re
import shutil
import time

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_openai import ChatOpenAI

from precondition_agent.ast_context import AstContextMixin
from precondition_agent.grounding import GroundingMixin
from precondition_agent.java_ast import JavaAstMixin
from precondition_agent.kotlin_ast import KotlinAstParser
from precondition_agent.probing import ProbingMixin
from precondition_agent.retrieval import RetrievalMixin
from precondition_agent.schemas import AnalysisConfig, GroundedSymbol, RetrievedContext, TraceEvent
from precondition_agent.synthesis import SynthesisMixin
from precondition_agent.utils import detect_kotlin_features, extract_json, infer_snippet_language, message_to_text


class PreconditionAnalysisAgent(JavaAstMixin, AstContextMixin, ProbingMixin, RetrievalMixin, GroundingMixin, SynthesisMixin):
    def __init__(self, config: AnalysisConfig):
        self.config = config
        llm_kwargs: Dict[str, Any] = {
            "model": config.model_name,
            "api_key": config.api_key,
            "base_url": config.base_url,
            "temperature": config.temperature,
        }
        if config.reasoning_effort:
            llm_kwargs["reasoning_effort"] = config.reasoning_effort
        if config.thinking_mode:
            llm_kwargs["extra_body"] = {"thinking": {"type": config.thinking_mode}}
        self.llm = ChatOpenAI(**llm_kwargs)
        self.trace: List[TraceEvent] = []
        self._file_cache: Dict[str, List[str]] = {}
        self._index_cache: Optional[Dict[str, str]] = None
        self._ast_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._ast_helper_ready: Optional[bool] = None
        self._ast_disabled_reason: str = ""
        self._repo_source_name_index: Dict[str, Dict[str, List[str]]] = {}
        self._kotlin_ast_parser = KotlinAstParser()
        self._current_snippet: str = ""

    def _trace(self, stage: str, payload: Dict[str, Any], started_at: float) -> None:
        if self.config.trace_level == "none":
            return
        if self.config.trace_stages and stage not in self.config.trace_stages:
            return
        self.trace.append(
            TraceEvent(
                stage=stage,
                payload=payload,
                duration_ms=int((time.time() - started_at) * 1000),
            )
        )

    def _compact_trace_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        compact: Dict[str, Any] = {}
        for key in [
            "status", "language", "engine", "entry_name", "symbol", "kind",
            "symbols_count", "contexts_count", "candidates", "selected",
            "hits", "count", "files", "names", "constraints_count",
            "direct_preconditions_count", "delegated_conflict_rules_count",
            "unknown_symbols_count",
        ]:
            if key in payload:
                compact[key] = payload[key]
        if "languages" in payload:
            compact["languages"] = payload["languages"]
        if "selected_blocks" in payload:
            compact["selected_blocks"] = payload["selected_blocks"]
        if "range" in payload:
            compact["range"] = payload["range"]
        if "source" in payload:
            compact["source"] = payload["source"]
        return compact or {"summary": "compact trace payload omitted"}

    def _serialize_trace(self) -> List[Dict[str, Any]]:
        if self.config.trace_level == "none":
            return []
        if self.config.trace_level == "compact":
            return [
                {
                    "stage": event.stage,
                    "payload": self._compact_trace_payload(event.payload),
                    "duration_ms": event.duration_ms,
                }
                for event in self.trace
            ]
        return [asdict(event) for event in self.trace]

    def _serialize_context_for_output(self, ctx: RetrievedContext) -> Dict[str, Any]:
        """Return context data for external output, including selected source code."""
        return {
            "symbol": ctx.symbol,
            "kind": ctx.kind,
            "query": ctx.query,
            "path": ctx.path,
            "anchor_line": ctx.anchor_line,
            "start_line": ctx.start_line,
            "end_line": ctx.end_line,
            "reason": ctx.reason,
            "source": ctx.source,
            "language": self._source_language_for_path(ctx.path),
            "chars": len(ctx.code),
            "code": ctx.code,
        }

    def _serialize_grounded_symbol(self, item: GroundedSymbol) -> Dict[str, Any]:
        return {
            "symbol": item.symbol,
            "kind": item.kind,
            "grounded_meaning": item.grounded_meaning,
            "role_in_snippet": item.role_in_snippet,
            "evidence": item.evidence,
            "confidence": item.confidence,
        }

    def close(self) -> None:
        pass

    def _resolve_rg(self) -> str:
        if os.path.isfile(self.config.rg_path):
            return self.config.rg_path

        resolved = shutil.which(self.config.rg_path)
        if resolved:
            return resolved

        raise FileNotFoundError(f"ripgrep not found: {self.config.rg_path}")

    def _load_lines(self, path: str) -> List[str]:
        if path not in self._file_cache:
            self._file_cache[path] = Path(path).read_text(
                encoding="utf-8",
                errors="ignore",
            ).splitlines()
        return self._file_cache[path]

    def _load_index(self) -> Dict[str, str]:
        if not self.config.index_file:
            return {}
        if self._index_cache is None:
            if not os.path.exists(self.config.index_file):
                self._index_cache = {}
                return self._index_cache
            with open(self.config.index_file, "r", encoding="utf-8") as f:
                self._index_cache = json.load(f)
        return self._index_cache

    def _resolve_index_path(self, raw_path: str) -> str:
        path = Path(raw_path)
        if path.is_absolute():
            return str(path)
        if self.config.index_file:
            base = Path(self.config.index_file).resolve().parent
            return str((base / path).resolve())
        return str(path.resolve())

    def _source_language_for_path(self, path: str) -> str:
        suffix = Path(path).suffix.lower()
        if suffix == ".java":
            return "java"
        if suffix == ".kt":
            return "kotlin"
        return "unknown"

    def _parse_source_file_with_ast(self, path: str) -> Optional[Dict[str, Any]]:
        language = self._source_language_for_path(path)
        if language == "java":
            return self._parse_java_file_with_ast(path)
        if language == "kotlin":
            return self._parse_kotlin_file_with_ast(path)

        started_at = time.time()
        self._trace(
            "ast_parse_file",
            {
                "status": "unsupported_language",
                "language": language,
                "path": path,
            },
            started_at,
        )
        return None

    def _parse_kotlin_file_with_ast(self, path: str) -> Optional[Dict[str, Any]]:
        started_at = time.time()
        if path in self._ast_cache:
            cached = self._ast_cache[path]
            self._trace(
                "ast_parse_file",
                {
                    "status": "cached",
                    "language": "kotlin",
                    "path": path,
                    "types": len((cached or {}).get("types", [])),
                    "methods": len((cached or {}).get("methods", [])),
                },
                started_at,
            )
            return cached

        if not self._kotlin_ast_parser.available():
            self._trace(
                "ast_parse_file",
                {
                    "status": "parser_unavailable",
                    "language": "kotlin",
                    "path": path,
                    "reason": self._kotlin_ast_parser.error or "unknown",
                },
                started_at,
            )
            self._ast_cache[path] = None
            return None

        try:
            parsed = self._kotlin_ast_parser.parse_file(path)
        except Exception as exc:
            self._trace(
                "ast_parse_file",
                {
                    "status": "parse_failed",
                    "language": "kotlin",
                    "path": path,
                    "error": str(exc),
                },
                started_at,
            )
            self._ast_cache[path] = None
            return None

        self._ast_cache[path] = parsed
        self._trace(
            "ast_parse_file",
            {
                "status": "ok",
                "language": "kotlin",
                "path": path,
                "types": len(parsed.get("types", [])),
                "methods": len(parsed.get("methods", [])),
            },
            started_at,
        )
        return parsed

    def _llm_json(self, prompt: str, default: Any) -> Any:
        started_at = time.time()
        response = self.llm.invoke(prompt)
        text = message_to_text(response)

        try:
            parsed = extract_json(text)
            self._trace(
                "llm_json",
                {
                    "status": "ok",
                    "chars": len(text),
                },
                started_at,
            )
            return parsed
        except Exception as exc:
            self._trace(
                "llm_json",
                {
                    "status": "fallback",
                    "chars": len(text),
                    "error": str(exc),
                },
                started_at,
            )
            return default



    def analyze_one(
        self,
        engine: str,
        repo_root: str,
        snippet: str,
        entry_name: str = "",
        index_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.trace = []
        self._file_cache = {}
        self._index_cache = None
        self._ast_disabled_reason = ""

        old_repo_root = self.config.repo_root
        old_index_file = self.config.index_file

        self.config.repo_root = repo_root
        self.config.index_file = index_file or self.config.index_file
        self._current_snippet = snippet

        try:
            started_at = time.time()
            probe_result = self.probe_symbols(snippet=snippet, entry_name=entry_name)
            contexts = self.retrieve_contexts(probe_result.get("symbols", []), repo_root=repo_root)
            grounded_symbols = self.ground_symbols(probe_result=probe_result, contexts=contexts)
            analysis = self.synthesize(
                engine=engine,
                snippet=snippet,
                probe_result=probe_result,
                contexts=contexts,
                grounded_symbols=grounded_symbols,
                entry_name=entry_name,
            )

            self._trace(
                "pipeline_complete",
                {
                    "engine": engine,
                    "entry_name": entry_name,
                    "contexts_count": len(contexts),
                },
                started_at,
            )

            return {
                "engine": engine,
                "entry_name": entry_name,
                "snippet_language": infer_snippet_language(snippet),
                "snippet_language_features": detect_kotlin_features(snippet),
                "probe_result": probe_result,
                "retrieved_contexts": [self._serialize_context_for_output(ctx) for ctx in contexts],
                "grounded_symbols": [self._serialize_grounded_symbol(item) for item in grounded_symbols],
                "analysis": analysis,
                "trace": self._serialize_trace(),
            }
        finally:
            self.config.repo_root = old_repo_root
            self.config.index_file = old_index_file
            self._current_snippet = ""

