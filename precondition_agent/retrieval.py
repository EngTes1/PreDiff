import json
import os
import re
import subprocess
import time

from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from precondition_agent.prompts import build_retrieval_rerank_prompt
from precondition_agent.schemas import RetrievedContext, RetrievalHit
from precondition_agent.utils import infer_snippet_language, load_text


class RetrievalMixin:
    def _select_direct_type_header_fast_path(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        kind = str(item.get("kind", "")).lower()
        role = str(item.get("role", "")).lower()
        if kind not in {"type", "class"} or role != "direct_guard":
            return []

        type_headers = [
            hit
            for hit in hits
            if hit.source == "ast" and (hit.metadata or {}).get("block_kind") == "type_header"
        ]
        if not type_headers:
            return []

        type_headers.sort(key=lambda hit: float(hit.score or 0.0), reverse=True)
        return [type_headers[0]]

    def _select_short_method_declaration_fast_path(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        kind = str(item.get("kind", "")).lower()
        if kind != "method":
            return []

        method_declarations = [
            hit
            for hit in hits
            if hit.source == "ast"
            and (hit.metadata or {}).get("block_kind") == "method_declaration"
            and hit.end_line is not None
        ]
        if not method_declarations:
            return []

        method_declarations.sort(key=lambda hit: float(hit.score or 0.0), reverse=True)
        full_method = method_declarations[0]
        method_length = int(full_method.end_line or full_method.line) - int(full_method.line) + 1
        if method_length > 30:
            return []

        has_nested_subblock = any(
            hit is not full_method
            and hit.source == "ast"
            and hit.path == full_method.path
            and hit.end_line is not None
            and int(full_method.line) <= int(hit.line)
            and int(hit.end_line) <= int(full_method.end_line or full_method.line)
            for hit in hits
        )
        return [full_method] if has_nested_subblock else []

    def _select_delegated_checker_method_fast_path(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        kind = str(item.get("kind", "")).lower()
        role = str(item.get("role", "")).lower()
        if kind != "method" or role != "delegated_checker":
            return []

        method_declarations = [
            hit
            for hit in hits
            if hit.source == "ast"
            and (hit.metadata or {}).get("block_kind") in {"method_declaration", "delegation_block"}
            and hit.end_line is not None
        ]
        if not method_declarations:
            return []

        method_declarations.sort(key=lambda hit: float(hit.score or 0.0), reverse=True)
        full_method = method_declarations[0]
        nested_subblocks = [
            hit
            for hit in hits
            if hit is not full_method
            and hit.source == "ast"
            and hit.path == full_method.path
            and hit.end_line is not None
            and int(full_method.line) <= int(hit.line)
            and int(hit.end_line) <= int(full_method.end_line or full_method.line)
        ]
        return [full_method] if nested_subblocks else []

    def _current_snippet_language(self) -> str:
        snippet = getattr(self, "_current_snippet", "") or ""
        return infer_snippet_language(snippet) if snippet else "unknown"

    def _preferred_source_globs(self) -> List[str]:
        language = self._current_snippet_language()
        if language == "java":
            return ["*.java"]
        if language == "kotlin":
            return ["*.kt"]
        return ["*.java", "*.kt"]

    def _fallback_source_globs(self) -> List[str]:
        preferred = self._preferred_source_globs()
        if preferred == ["*.java"]:
            return ["*.kt"]
        if preferred == ["*.kt"]:
            return ["*.java"]
        return []

    def _candidate_suffixes(self) -> List[str]:
        language = self._current_snippet_language()
        if language == "java":
            return [".java", ".kt"]
        if language == "kotlin":
            return [".kt", ".java"]
        return [".java", ".kt"]

    def _language_score_adjustment(self, path: str) -> float:
        path_language = self._source_language_for_path(path)
        snippet_language = self._current_snippet_language()
        if snippet_language == "unknown" or path_language == "unknown":
            return 0.0
        if path_language == snippet_language:
            return 12.0
        return -6.0

    def _call_arity_score_adjustment(self, item: Dict[str, Any], method_item: Dict[str, Any]) -> float:
        call_arity = item.get("call_arity")
        try:
            call_arity_int = int(call_arity)
        except Exception:
            return 0.0

        parameter_types = method_item.get("parameter_types", [])
        if not isinstance(parameter_types, list):
            return 0.0
        declared_arity = len(parameter_types)
        if declared_arity == call_arity_int:
            return 85.0
        return -45.0

    def _is_within_repo(self, path: str, repo_root: str) -> bool:
        if not repo_root:
            return True
        try:
            repo = Path(repo_root).resolve()
            target = Path(path).resolve()
            return repo == target or repo in target.parents
        except Exception:
            return False

    def _repo_index_cache_path(self, repo_root: str) -> Path:
        normalized_root = str(Path(repo_root).resolve())
        cache_dir = Path(self.config.workspace_cache_dir)
        if not cache_dir.is_absolute():
            cache_dir = Path.cwd() / cache_dir
        cache_dir = cache_dir / "repo_source_indexes"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = sha1(normalized_root.encode("utf-8")).hexdigest()[:16]
        repo_name = Path(normalized_root).name or "repo"
        return cache_dir / f"{repo_name}-{digest}.json"

    def _load_repo_source_name_index(self, repo_root: str) -> Dict[str, List[str]]:
        started_at = time.time()
        normalized_root = str(Path(repo_root).resolve())
        if normalized_root in self._repo_source_name_index:
            cached = self._repo_source_name_index[normalized_root]
            self._trace(
                "repo_source_name_index",
                {
                    "repo_root": normalized_root,
                    "status": "cached",
                    "languages": ["java", "kotlin"],
                    "files": sum(len(paths) for paths in cached.values()),
                    "names": len(cached),
                },
                started_at,
            )
            return cached

        cache_path = self._repo_index_cache_path(normalized_root)
        if self.config.enable_repo_source_cache and cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("index"), dict):
                    cached_index = {
                        str(name): [str(path) for path in paths if isinstance(path, str)]
                        for name, paths in payload["index"].items()
                        if isinstance(paths, list)
                    }
                    self._repo_source_name_index[normalized_root] = cached_index
                    self._trace(
                        "repo_source_name_index",
                        {
                            "repo_root": normalized_root,
                            "status": "disk_cached",
                            "languages": ["java", "kotlin"],
                            "files": sum(len(paths) for paths in cached_index.values()),
                            "names": len(cached_index),
                            "cache_path": str(cache_path),
                        },
                        started_at,
                    )
                    return cached_index
            except Exception:
                pass

        index: Dict[str, List[str]] = {}

        try:
            rg = self._resolve_rg()
            process = subprocess.run(
                [
                    rg,
                    "--files",
                    "--glob",
                    "*.java",
                    "--glob",
                    "*.kt",
                    "--glob",
                    "!**/testData/**",
                    "--glob",
                    "!**/tests/**",
                    "--glob",
                    "!**/test/**",
                    "--glob",
                    "!**/java-tests/**",
                    normalized_root,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            if process.returncode == 0:
                for line in process.stdout.splitlines():
                    if not line:
                        continue
                    file_path = str(Path(line).resolve())
                    base_name = Path(file_path).name
                    index.setdefault(base_name, []).append(file_path)
        except Exception:
            index = {}

        if not index:
            for pattern in ["*.java", "*.kt"]:
                for file_path in Path(normalized_root).rglob(pattern):
                    if self._is_excluded_path(str(file_path)):
                        continue
                    resolved = str(file_path.resolve())
                    index.setdefault(file_path.name, []).append(resolved)

        self._repo_source_name_index[normalized_root] = index
        if self.config.enable_repo_source_cache:
            try:
                cache_path.write_text(
                    json.dumps({"repo_root": normalized_root, "index": index}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        self._trace(
            "repo_source_name_index",
            {
                "repo_root": normalized_root,
                "status": "built",
                "languages": ["java", "kotlin"],
                "files": sum(len(paths) for paths in index.values()),
                "names": len(index),
                "cache_path": str(cache_path) if self.config.enable_repo_source_cache else "",
            },
            started_at,
        )
        return index

    def _collect_ast_candidate_paths(
        self,
        item: Dict[str, Any],
        repo_root: str,
        fallback_hits: List[RetrievalHit],
    ) -> List[str]:
        started_at = time.time()
        symbol = item.get("symbol", "")
        kind = (item.get("kind") or "").lower()
        candidate_paths: List[str] = []
        repo_name_index = self._load_repo_source_name_index(repo_root) if repo_root else {}

        if kind in {"class", "type"} and symbol:
            index = self._load_index()
            raw_path = index.get(symbol.split(".")[-1])
            if raw_path:
                resolved = self._resolve_index_path(raw_path)
                if os.path.exists(resolved) and self._is_within_repo(resolved, repo_root):
                    candidate_paths.append(resolved)
            for suffix in self._candidate_suffixes():
                for resolved in repo_name_index.get(f"{symbol.split('.')[-1]}{suffix}", []):
                    if self._is_within_repo(resolved, repo_root):
                        candidate_paths.append(resolved)

        if kind == "method" and "." in symbol:
            owner_symbol = symbol.rsplit(".", 1)[0].split(".")[-1]
            index = self._load_index()
            raw_path = index.get(owner_symbol)
            if raw_path:
                resolved = self._resolve_index_path(raw_path)
                if os.path.exists(resolved) and self._is_within_repo(resolved, repo_root):
                    candidate_paths.append(resolved)
            for suffix in self._candidate_suffixes():
                for resolved in repo_name_index.get(f"{owner_symbol}{suffix}", []):
                    if self._is_within_repo(resolved, repo_root):
                        candidate_paths.append(resolved)

        for hit in fallback_hits:
            if hit.path and self._is_within_repo(hit.path, repo_root):
                candidate_paths.append(hit.path)

        deduped: List[str] = []
        seen = set()
        for path in candidate_paths:
            normalized = str(Path(path).resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
            if len(deduped) >= max(1, int(self.config.max_candidate_paths_per_symbol)):
                break

        self._trace(
            "ast_candidate_paths",
            {
                "symbol": symbol,
                "kind": kind,
                "count": len(deduped),
                "paths": deduped[:6],
                "languages": sorted({self._source_language_for_path(path) for path in deduped}),
            },
            started_at,
        )
        return deduped

    def _search_with_ast(
        self,
        item: Dict[str, Any],
        repo_root: str,
        fallback_hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        started_at = time.time()
        symbol = item.get("symbol", "")
        kind = (item.get("kind") or "").lower()
        target_name = symbol.split(".")[-1]
        candidate_paths = self._collect_ast_candidate_paths(item, repo_root, fallback_hits)
        hits: List[RetrievalHit] = []
        parsed_languages: Dict[str, int] = {}

        if not target_name or kind not in {"class", "type", "method", "field", "property"}:
            self._trace(
                "ast_search",
                {
                    "symbol": symbol,
                    "kind": kind,
                    "hits": 0,
                    "status": "unsupported_symbol",
                    "languages": [],
                },
                started_at,
            )
            return hits

        for path in candidate_paths:
            parsed = self._parse_source_file_with_ast(path)
            if not parsed:
                continue
            language = str(parsed.get("language") or self._source_language_for_path(path))
            parsed_languages[language] = parsed_languages.get(language, 0) + 1

            if kind in {"class", "type"}:
                for type_item in parsed.get("types", []):
                    if type_item.get("name") != target_name:
                        continue
                    hits.extend(
                        self._build_type_context_hits(
                            item=item,
                            path=path,
                            parsed=parsed,
                            type_item=type_item,
                        )
                    )

                if not hits:
                    for method_item in parsed.get("methods", []):
                        type_refs = method_item.get("type_refs", [])
                        instanceof_types = method_item.get("instanceof_types", [])
                        if target_name not in type_refs and target_name not in instanceof_types:
                            continue
                        start_line = int(method_item.get("start_line", 1))
                        end_line = int(method_item.get("end_line", start_line))
                        hits.append(
                            RetrievalHit(
                                path=path,
                                line=start_line,
                                text=f"{method_item.get('owner_type', '')}.{method_item.get('name', '')}",
                                source="ast",
                                score=165.0,
                                preview=self._make_preview(path, start_line, radius=3),
                                end_line=end_line,
                                node_kind="METHOD",
                                metadata={
                                    "match_kind": "type_reference_method",
                                    "block_kind": "type_reference_method",
                                    "semantic_block_type": "type_reference_block",
                                    "language": language,
                                },
                            )
                        )

            if kind == "method":
                declaration_found = False
                for method_item in parsed.get("methods", []):
                    start_line = int(method_item.get("start_line", 1))
                    end_line = int(method_item.get("end_line", start_line))
                    if method_item.get("name") == target_name:
                        declaration_found = True
                        block_kind = self._classify_method_context_block(
                            method_item=method_item,
                            language=language,
                            default="method_declaration",
                        )
                        hits.append(
                            RetrievalHit(
                                path=path,
                                line=start_line,
                                text=f"{method_item.get('owner_type', '')}.{target_name}()",
                                source="ast",
                                score=230.0 + self._call_arity_score_adjustment(item, method_item),
                                preview=self._make_preview(path, start_line, radius=3),
                                end_line=end_line,
                                node_kind="METHOD",
                                metadata={
                                    "match_kind": "method_declaration",
                                    "block_kind": block_kind,
                                    "semantic_block_type": block_kind,
                                    "block_label": f"{method_item.get('owner_type', '')}.{target_name}()".strip("."),
                                    "summary": self._build_method_block_summary(method_item, method_item.get("owner_type", "")),
                                    "signature": self._build_method_signature(method_item, method_item.get("owner_type", "")),
                                    "return_type": method_item.get("return_type", ""),
                                    "parameter_types": method_item.get("parameter_types", []),
                                    "extension_receiver": method_item.get("extension_receiver", ""),
                                    "kotlin_features": method_item.get("kotlin_features", []),
                                    "conditions_count": len(method_item.get("conditions", [])),
                                    "called_methods_count": len(method_item.get("called_methods", [])),
                                    "language": language,
                                },
                            )
                        )
                        hits.extend(
                            self._build_method_sub_block_hits(
                                item=item,
                                path=path,
                                method_item=method_item,
                                type_name=method_item.get("owner_type", "") or target_name,
                                base_score=self._score_semantic_method_block(
                                    item,
                                    method_item,
                                    method_item.get("owner_type", "") or target_name,
                                )
                                + self._call_arity_score_adjustment(item, method_item),
                            )
                        )
                if not declaration_found:
                    for method_item in parsed.get("methods", []):
                        start_line = int(method_item.get("start_line", 1))
                        end_line = int(method_item.get("end_line", start_line))
                        called_methods = method_item.get("called_methods", [])
                        if target_name in called_methods:
                            hits.append(
                                RetrievalHit(
                                    path=path,
                                    line=start_line,
                                    text=f"{method_item.get('owner_type', '')}.{method_item.get('name', '')}()",
                                    source="ast",
                                    score=150.0,
                                    preview=self._make_preview(path, start_line, radius=3),
                                    end_line=end_line,
                                    node_kind="METHOD",
                                    metadata={
                                        "match_kind": "method_call_container",
                                        "block_kind": "call_container",
                                        "semantic_block_type": "call_site_container_block",
                                        "block_label": f"{method_item.get('owner_type', '')}.{method_item.get('name', '')}()".strip("."),
                                        "summary": self._build_method_block_summary(method_item, method_item.get("owner_type", "")),
                                        "kotlin_features": method_item.get("kotlin_features", []),
                                        "conditions_count": len(method_item.get("conditions", [])),
                                        "called_methods_count": len(method_item.get("called_methods", [])),
                                        "language": language,
                                    },
                                )
                            )

            if kind in {"field", "property"}:
                for property_item in parsed.get("properties", []):
                    if property_item.get("name") != target_name:
                        continue
                    start_line = int(property_item.get("start_line", 1))
                    end_line = int(property_item.get("end_line", start_line))
                    hits.append(
                        RetrievalHit(
                            path=path,
                            line=start_line,
                            text=f"{property_item.get('owner_type', '')}.{target_name}".strip("."),
                            source="ast",
                            score=205.0,
                            preview=self._make_preview(path, start_line, radius=3),
                            end_line=end_line,
                            node_kind="PROPERTY",
                            metadata={
                                "match_kind": "property_declaration",
                                "block_kind": "property_getter" if property_item.get("has_getter") else "property_declaration",
                                "semantic_block_type": "property_semantics_block",
                                "block_label": f"{property_item.get('owner_type', '')}.{target_name}".strip("."),
                                "summary": self._build_property_block_summary(property_item),
                                "return_type": property_item.get("return_type", ""),
                                "kotlin_features": property_item.get("kotlin_features", []),
                                "conditions_count": len(property_item.get("conditions", [])),
                                "called_methods_count": len(property_item.get("called_methods", [])),
                                "language": language,
                                },
                            )
                        )

        for hit in hits:
            hit.score += self._language_score_adjustment(hit.path)

        hits = self._deduplicate_hits(hits)[: self.config.max_search_hits_per_query]
        self._trace(
            "ast_search",
            {
                "symbol": symbol,
                "kind": kind,
                "hits": len(hits),
                "status": "ok" if hits else "no_hits",
                "languages": parsed_languages,
            },
            started_at,
        )
        return hits

    def retrieve_contexts(
        self,
        symbols: List[Dict[str, Any]],
        repo_root: Optional[str] = None,
    ) -> List[RetrievedContext]:
        started_at = time.time()
        repo_root = repo_root or self.config.repo_root
        contexts: List[RetrievedContext] = []
        seen_blocks = set()

        for item in symbols:
            if len(contexts) >= self.config.max_total_contexts:
                break

            ast_hits = self._search_with_ast(item, repo_root, [])
            fallback_hits: List[RetrievalHit] = []

            if not ast_hits:
                per_query_limit = min(
                    self.config.max_search_hits_per_query,
                    int(item.get("top_k", self.config.max_search_hits_per_query)),
                )
                for query in self._expand_queries(item):
                    fallback_hits.extend(
                        self.search_repo(
                            query=query,
                            repo_root=repo_root,
                            max_results=per_query_limit,
                            symbol=item.get("symbol", ""),
                            kind=item.get("kind", ""),
                            source_globs=self._preferred_source_globs(),
                        )
                    )
                if not fallback_hits:
                    fallback_globs = self._fallback_source_globs()
                    for query in self._expand_queries(item):
                        if not fallback_globs:
                            break
                        fallback_hits.extend(
                            self.search_repo(
                                query=query,
                                repo_root=repo_root,
                                max_results=per_query_limit,
                                symbol=item.get("symbol", ""),
                                kind=item.get("kind", ""),
                                source_globs=fallback_globs,
                            )
                        )
                fallback_hits = self._deduplicate_hits(fallback_hits)
                ast_hits = self._search_with_ast(item, repo_root, fallback_hits)

            hits = ast_hits or self._deduplicate_hits(fallback_hits)[: self.config.max_search_hits_per_query]

            if not hits:
                continue

            chosen_hits = self._choose_hits(item, hits)

            for hit in chosen_hits[: self.config.max_contexts_per_symbol]:
                context = self.read_adaptive_context(
                    path=hit.path,
                    anchor_line=hit.line,
                    symbol=item.get("symbol", ""),
                    kind=item.get("kind", "other"),
                    region=item.get("region", "any"),
                    reason=item.get("reason", ""),
                    query=item.get("query", ""),
                    source=hit.source,
                    exact_start_line=hit.line if hit.source == "ast" else None,
                    exact_end_line=hit.end_line if hit.source == "ast" else None,
                    hit_metadata=hit.metadata,
                )
                block_key = (context.path, context.start_line, context.end_line, context.symbol)
                if block_key in seen_blocks:
                    continue

                seen_blocks.add(block_key)
                contexts.append(context)

                if len(contexts) >= self.config.max_total_contexts:
                    break

        self._trace("context_retrieval", {"symbols_count": len(symbols), "contexts_count": len(contexts)}, started_at)
        return contexts

    def _choose_hits(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        if len(hits) <= 1:
            return hits

        fast_selected = self._select_direct_type_header_fast_path(item, hits)
        if fast_selected:
            started_at = time.time()
            self._trace(
                "context_planning_fast_path",
                {
                    "symbol": item.get("symbol", ""),
                    "selected": len(fast_selected),
                    "selected_blocks": [hit.metadata.get("block_kind", "") for hit in fast_selected],
                    "reason": "direct_guard_type_header_only",
                },
                started_at,
            )
            return fast_selected

        fast_selected = self._select_short_method_declaration_fast_path(item, hits)
        if fast_selected:
            started_at = time.time()
            self._trace(
                "context_planning_fast_path",
                {
                    "symbol": item.get("symbol", ""),
                    "selected": len(fast_selected),
                    "selected_blocks": [hit.metadata.get("block_kind", "") for hit in fast_selected],
                    "reason": "short_method_declaration_only",
                },
                started_at,
            )
            return fast_selected

        fast_selected = self._select_delegated_checker_method_fast_path(item, hits)
        if fast_selected:
            started_at = time.time()
            self._trace(
                "context_planning_fast_path",
                {
                    "symbol": item.get("symbol", ""),
                    "selected": len(fast_selected),
                    "selected_blocks": [hit.metadata.get("block_kind", "") for hit in fast_selected],
                    "reason": "delegated_checker_whole_method",
                },
                started_at,
            )
            return fast_selected

        top_hit = hits[0]
        if top_hit.source == "ast":
            same_source_hits = sorted(
                [hit for hit in hits if hit.source == "ast"],
                key=lambda hit: float(hit.score or 0.0),
                reverse=True,
            )[: max(1, int(self.config.max_ast_candidates_for_planning))]
            if len(same_source_hits) == 1:
                return same_source_hits or [top_hit]
            return self._plan_ast_contexts_with_llm(item, same_source_hits)

        return self.select_hits_with_llm(item, hits)

    def _expand_queries(self, item: Dict[str, Any]) -> List[str]:
        """Generate stable repository queries for type and static-method retrieval."""
        symbol = item.get("symbol", "")
        kind = (item.get("kind") or "").lower()
        base_query = item.get("query", "")
        queries: List[str] = []

        if base_query:
            queries.append(base_query)

        if kind in {"class", "type"} and symbol:
            queries = [
                rf"interface\s+{re.escape(symbol)}\b",
                rf"class\s+{re.escape(symbol)}\b",
                rf"object\s+{re.escape(symbol)}\b",
                rf"enum\s+{re.escape(symbol)}\b",
            ]

        if kind == "method" and "." in symbol:
            method_name = symbol.split(".")[-1]
            queries.extend(
                [
                    rf"\b{re.escape(method_name)}\s*\(",
                    rf"(public|protected|private)\s+.*\b{re.escape(method_name)}\s*\(",
                    rf"\bfun\s+.*\b{re.escape(method_name)}\s*\(",
                ]
            )

        if kind in {"field", "property"} and symbol:
            queries.extend(
                [
                    rf"\b(val|var)\s+{re.escape(symbol)}\b",
                    rf"\b{re.escape(symbol)}\b",
                ]
            )

        ordered = []
        seen = set()
        for query in queries:
            if query and query not in seen:
                seen.add(query)
                ordered.append(query)
        return ordered

    def _deduplicate_hits(self, hits: List[RetrievalHit]) -> List[RetrievalHit]:
        deduped: List[RetrievalHit] = []
        seen = set()
        for hit in sorted(hits, key=lambda item: item.score, reverse=True):
            key = (hit.path, hit.line)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hit)
        return deduped

    def search_repo(
        self,
        query: str,
        repo_root: str,
        max_results: int,
        symbol: str,
        kind: str,
        source_globs: Optional[List[str]] = None,
    ) -> List[RetrievalHit]:
        started_at = time.time()
        rg = self._resolve_rg()
        source_globs = source_globs or ["*.java", "*.kt"]

        cmd = [
            rg,
            "-n",
            "--no-heading",
            "--color",
            "never",
            query,
            repo_root,
        ]
        glob_args: List[str] = []
        for glob in source_globs:
            glob_args.extend(["--glob", glob])
        glob_args.extend(
            [
                "--glob",
                "!**/testData/**",
                "--glob",
                "!**/tests/**",
                "--glob",
                "!**/test/**",
                "--glob",
                "!**/java-tests/**",
            ]
        )
        cmd = cmd[:6] + glob_args + cmd[6:]

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )

        hits: List[RetrievalHit] = []
        if process.returncode not in (0, 1):
            self._trace(
                "search_repo",
                {
                    "query": query,
                    "repo_root": repo_root,
                    "source_globs": source_globs,
                    "error": (process.stderr or "rg failed").strip(),
                },
                started_at,
            )
            return hits

        for line in process.stdout.splitlines():
            match = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not match:
                continue

            path = match.group(1)
            if self._is_excluded_path(path):
                continue
            line_no = int(match.group(2))
            text = match.group(3)
            preview = self._make_preview(path, line_no, radius=3)
            score = self._score_hit(symbol=symbol, kind=kind, path=path, text=text)
            hits.append(
                RetrievalHit(
                    path=path,
                    line=line_no,
                    text=text,
                    source="rg",
                    score=score,
                    preview=preview,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        hits = hits[:max_results]

        self._trace(
            "search_repo",
            {
                "query": query,
                "repo_root": repo_root,
                "source_globs": source_globs,
                "hits": len(hits),
            },
            started_at,
        )
        return hits

    def _is_excluded_path(self, path: str) -> bool:
        path_lower = path.replace("\\", "/").lower()
        excluded_parts = [
            "/test/",
            "/tests/",
            "/testdata/",
            "/java-tests/",
            "/mock/",
            "/mocks/",
            "/fixtures/",
        ]
        return any(part in path_lower for part in excluded_parts)

    def _score_hit(self, symbol: str, kind: str, path: str, text: str) -> float:
        score = 0.0
        path_lower = path.replace("\\", "/").lower()
        text_lower = text.lower()
        escaped_symbol = re.escape(symbol)

        if kind in {"class", "type"} and re.search(rf"\b(class|interface|object|enum)\s+{escaped_symbol}\b", text):
            score += 100
        if kind == "method" and re.search(rf"\b{escaped_symbol}\s*\(", text):
            score += 40
        if kind in {"field", "property"} and re.search(rf"\b(val|var)\s+{escaped_symbol}\b", text):
            score += 80
        score += self._language_score_adjustment(path)
        if any(token in text_lower for token in ["public ", "private ", "protected ", "internal ", "override "]):
            score += 12
        if " static " in f" {text_lower} ":
            score += 6
        if "class " in text_lower or "interface " in text_lower or "object " in text_lower or "enum " in text_lower:
            score += 25
        if "fun " in text_lower:
            score += 12
        if "return " in text_lower or "if (" in text_lower or "if " in text_lower or "when " in text_lower:
            score += 5

        for good, bonus in [
            ("/refactor", 8),
            ("/refactoring", 8),
            ("/rename", 6),
            ("/psi/", 5),
            ("/util/", 4),
            ("/search/", 4),
        ]:
            if good in path_lower:
                score += bonus

        for bad, penalty in [
            ("/test/", 10),
            ("/tests/", 10),
            ("/testdata/", 20),
            ("/java-tests/", 20),
            ("/demo", 6),
            ("/example", 6),
        ]:
            if bad in path_lower:
                score -= penalty

        return score

    def _make_preview(self, path: str, line_no: int, radius: int = 3) -> str:
        try:
            lines = self._load_lines(path)
        except Exception:
            return ""

        start = max(0, line_no - 1 - radius)
        end = min(len(lines), line_no - 1 + radius + 1)
        buf = []
        for index in range(start, end):
            buf.append(f"L{index + 1}: {lines[index]}")
        return "\n".join(buf)

    def select_hits_with_llm(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        if len(hits) <= 1:
            return hits

        started_at = time.time()
        candidates = []
        for idx, hit in enumerate(hits[:8]):
            candidates.append(
                {
                    "id": idx,
                    "path": hit.path,
                    "line": hit.line,
                    "end_line": hit.end_line,
                    "text": hit.text,
                    "preview": hit.preview,
                    "score": hit.score,
                    "source": hit.source,
                    "node_kind": hit.node_kind,
                    "metadata": hit.metadata,
                }
            )

        prompt = build_retrieval_rerank_prompt(item, candidates)

        result = self._llm_json(prompt, {"selected_ids": [0], "reason": "fallback"})
        selected_ids = result.get("selected_ids", [0])

        selected: List[RetrievalHit] = []
        for idx in selected_ids:
            if isinstance(idx, int) and 0 <= idx < len(hits):
                selected.append(hits[idx])

        if not selected:
            selected = hits[:1]

        self._trace(
            "retrieval_rerank",
            {
                "symbol": item.get("symbol", ""),
                "candidates": len(candidates),
                "selected": len(selected),
                "selected_sources": [hit.source for hit in selected],
                "selected_blocks": [hit.metadata.get("block_kind", "") for hit in selected],
            },
            started_at,
        )
        return selected

    def read_adaptive_context(
        self,
        path: str,
        anchor_line: int,
        symbol: str,
        kind: str,
        region: str,
        reason: str,
        query: str,
        source: str,
        exact_start_line: Optional[int] = None,
        exact_end_line: Optional[int] = None,
        hit_metadata: Optional[Dict[str, Any]] = None,
    ) -> RetrievedContext:
        started_at = time.time()
        lines = self._load_lines(path)
        anchor_idx = max(0, min(len(lines) - 1, anchor_line - 1))
        hit_metadata = hit_metadata or {}
        context_strategy = hit_metadata.get("context_strategy", "")
        strategy_details: Dict[str, Any] = {}

        if exact_start_line is not None and exact_end_line is not None:
            selected_start, selected_end, context_strategy, strategy_details = self._select_ast_context_range(
                path=path,
                symbol=symbol,
                kind=kind,
                exact_start_line=exact_start_line,
                exact_end_line=exact_end_line,
                hit_metadata=hit_metadata,
            )
            start_idx = max(0, min(len(lines) - 1, selected_start - 1))
            end_idx = max(start_idx, min(len(lines) - 1, selected_end - 1))
        else:
            start_idx, end_idx = self._locate_best_block(
                lines=lines,
                anchor_idx=anchor_idx,
                symbol=symbol,
                kind=kind,
                region=region,
            )

        code = "\n".join(lines[start_idx:end_idx + 1])

        self._trace(
            "read_context",
            {
                "path": path,
                "symbol": symbol,
                "region": region,
                "source": source,
                "language": hit_metadata.get("language", self._source_language_for_path(path)),
                "range": f"{start_idx + 1}-{end_idx + 1}",
                "exact_range": bool(exact_start_line is not None and exact_end_line is not None),
                "context_strategy": context_strategy,
                "strategy_details": strategy_details,
            },
            started_at,
        )

        return RetrievedContext(
            symbol=symbol,
            kind=kind,
            query=query,
            path=path,
            anchor_line=anchor_line,
            start_line=start_idx + 1,
            end_line=end_idx + 1,
            reason=reason,
            source=source,
            code=code,
        )

    def _locate_best_block(
        self,
        lines: List[str],
        anchor_idx: int,
        symbol: str,
        kind: str,
        region: str,
    ) -> Tuple[int, int]:
        # Try to recover a full method body or type definition instead of a fixed-size window.
        if kind in {"class", "type"}:
            start = self._find_type_decl(lines, anchor_idx, symbol)
            if start is not None:
                end = self._find_brace_block_end(lines, start)
                if end is not None:
                    return start, min(end, start + 80)
        if kind == "method" or region == "method_body":
            start = self._find_method_decl(lines, anchor_idx, symbol)
            if start is not None:
                end = self._find_brace_block_end(lines, start)
                if end is not None:
                    return start, min(end, start + 140)

        window_sizes = {
            "definition": (20, 60),
            "method_body": (20, 120),
            "call_site": (12, 25),
            "any": (18, 50),
        }
        before, after = window_sizes.get(region, (18, 50))
        start = max(0, anchor_idx - before)
        end = min(len(lines) - 1, anchor_idx + after)
        return start, end

    def _find_type_decl(self, lines: List[str], anchor_idx: int, symbol: str) -> Optional[int]:
        pattern = re.compile(rf"\b(class|interface|object|enum)\s+{re.escape(symbol)}\b")
        for idx in range(anchor_idx, max(-1, anchor_idx - 80), -1):
            if pattern.search(lines[idx]):
                return idx
        for idx in range(anchor_idx, min(len(lines), anchor_idx + 20)):
            if pattern.search(lines[idx]):
                return idx
        return None

    def _find_method_decl(self, lines: List[str], anchor_idx: int, symbol: str) -> Optional[int]:
        pattern = re.compile(rf"(?:\bfun\s+.*\b{re.escape(symbol)}\s*\(|\b{re.escape(symbol)}\s*\()")
        start = max(0, anchor_idx - 80)
        end = min(len(lines), anchor_idx + 20)

        for idx in range(anchor_idx, start - 1, -1):
            if pattern.search(lines[idx]):
                return idx
        for idx in range(anchor_idx + 1, end):
            if pattern.search(lines[idx]):
                return idx
        return None

    def _find_brace_block_end(self, lines: List[str], start_idx: int) -> Optional[int]:
        brace_count = 0
        opened = False

        for idx in range(start_idx, min(len(lines), start_idx + 400)):
            line = lines[idx]
            for ch in line:
                if ch == "{":
                    brace_count += 1
                    opened = True
                elif ch == "}":
                    brace_count -= 1
            if opened and brace_count == 0:
                return idx
        return None

    def read_java_by_class(self, class_name: str) -> Optional[str]:
        started_at = time.time()
        if not self.config.index_file:
            self._trace(
                "read_java_by_class",
                {"class_name": class_name, "status": "no_index"},
                started_at,
            )
            return None

        index = self._load_index()
        raw_path = index.get(class_name)
        if not raw_path:
            self._trace(
                "read_java_by_class",
                {"class_name": class_name, "status": "not_found"},
                started_at,
            )
            return None

        resolved = self._resolve_index_path(raw_path)
        if not os.path.exists(resolved):
            self._trace(
                "read_java_by_class",
                {"class_name": class_name, "status": "path_missing", "path": resolved},
                started_at,
            )
            return None

        text = load_text(resolved)
        self._trace(
            "read_java_by_class",
            {
                "class_name": class_name,
                "status": "ok",
                "path": resolved,
                "chars": len(text),
            },
            started_at,
        )
        return text

    # -------------------------
    # Stage 3: Semantic Synthesis
    # -------------------------

