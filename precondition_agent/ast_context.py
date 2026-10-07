import re
import time

from typing import Any, Dict, List, Tuple

from precondition_agent.language_rules.common_rules import HELPER_NAME_HINTS
from precondition_agent.language_rules.context_rules import (
    CONSTRUCTION_REASON_HINTS,
    CONSTRUCTOR_REASON_HINTS,
    METHOD_NAME_SCORE_HINTS,
    SEMANTIC_CONTEXT_BLOCK_KINDS,
    SUB_BLOCK_PATTERNS,
)
from precondition_agent.prompts import build_ast_context_planning_prompt
from precondition_agent.schemas import RetrievalHit


class AstContextMixin:
    def _make_ast_hit(
        self,
        path: str,
        start_line: int,
        end_line: int,
        score: float,
        text: str,
        node_kind: str,
        metadata: Dict[str, Any],
    ) -> RetrievalHit:
        metadata = dict(metadata)
        metadata.setdefault("language", self._source_language_for_path(path))
        return RetrievalHit(
            path=path,
            line=start_line,
            text=text,
            source="ast",
            score=score,
            preview=self._make_preview(path, start_line, radius=3),
            end_line=end_line,
            node_kind=node_kind,
            metadata=metadata,
        )

    def _extract_reason_tokens(self, item: Dict[str, Any]) -> List[str]:
        reason = str(item.get("reason", "")).lower()
        tokens = re.findall(r"[a-z_][a-z0-9_]{2,}", reason)
        stopwords = {
            "the", "and", "for", "with", "that", "this", "from", "into", "what",
            "when", "where", "which", "while", "only", "real", "code", "logic",
            "current", "condition", "check", "determines", "prevents", "processing",
            "instance", "method", "type", "symbol", "boolean", "decision", "reason",
            "used", "using", "affects", "affect", "structure", "semantic", "semantics",
            "context", "conflict", "conflicts", "detection", "trigger", "triggers",
        }
        ordered: List[str] = []
        seen = set()
        for token in tokens:
            if token in stopwords or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[:12]

    def _build_method_signature(self, method_item: Dict[str, Any], owner_type: str) -> str:
        name = method_item.get("name", "")
        parameter_types = method_item.get("parameter_types", [])
        if not isinstance(parameter_types, list):
            parameter_types = []
        params = ", ".join(str(param) for param in parameter_types if str(param).strip())
        if method_item.get("is_constructor"):
            return f"{owner_type}({params})"
        return_type = str(method_item.get("return_type", "")).strip()
        receiver = str(method_item.get("extension_receiver", "")).strip()
        qualified_name = f"{receiver}.{name}" if receiver else name
        signature = f"{qualified_name}({params})"
        return f"{return_type} {signature}".strip() if return_type else signature

    def _semantic_method_reason_overlap(
        self,
        item: Dict[str, Any],
        method_item: Dict[str, Any],
    ) -> int:
        reason_tokens = self._extract_reason_tokens(item)
        surface = " ".join(
            [
                str(method_item.get("name", "")).lower(),
                str(method_item.get("return_type", "")).lower(),
                " ".join(str(param).lower() for param in method_item.get("parameter_types", [])),
                " ".join(str(ref).lower() for ref in method_item.get("type_refs", [])),
                " ".join(str(cond).lower() for cond in method_item.get("conditions", [])),
                " ".join(str(feature).lower() for feature in method_item.get("kotlin_features", [])),
                str(method_item.get("extension_receiver", "")).lower(),
            ]
        )
        overlap = 0
        for token in reason_tokens:
            if token in surface:
                overlap += 1
        return overlap

    def _score_reason_overlap_text(self, item: Dict[str, Any], text: str) -> int:
        lowered = text.lower()
        overlap = 0
        for token in self._extract_reason_tokens(item):
            if token in lowered:
                overlap += 1
        return overlap

    def _score_semantic_method_block(
        self,
        item: Dict[str, Any],
        method_item: Dict[str, Any],
        type_name: str,
    ) -> float:
        score = 0.0
        conditions = method_item.get("conditions", [])
        called_methods = method_item.get("called_methods", [])
        modifiers = method_item.get("modifiers", [])
        kotlin_features = method_item.get("kotlin_features", [])
        name_lower = str(method_item.get("name", "")).lower()
        overlap_count = self._semantic_method_reason_overlap(item, method_item)

        score += overlap_count * 8
        score += min(len(conditions), 3) * 6
        score += min(len(called_methods), 3) * 3
        score += min(len(kotlin_features), 4) * 5

        for keyword, bonus in METHOD_NAME_SCORE_HINTS:
            if keyword in name_lower:
                score += bonus

        if name_lower in {"equals", "hashcode", "tostring"}:
            score -= 18
        if name_lower.startswith("set"):
            score -= 10
        if "icon" in name_lower or "presentation" in name_lower:
            score -= 10
        if name_lower.startswith("get") and not conditions and not called_methods:
            score -= 6
        if "default" in modifiers:
            score -= 6
        if method_item.get("is_constructor"):
            score += 12
        if method_item.get("is_extension"):
            score += 10
        if method_item.get("is_top_level"):
            score += 6
        if any(feature in kotlin_features for feature in ["safe_cast", "elvis_return", "type_check", "when"]):
            score += 14
        if type_name.lower().startswith("light") and "light" in name_lower:
            score += 5
        return score

    def _build_type_header_summary(self, type_item: Dict[str, Any]) -> str:
        modifiers = ", ".join(type_item.get("modifiers", [])[:3])
        extends_types = ", ".join(type_item.get("extends_types", [])[:2])
        implements_types = ", ".join(type_item.get("implements_types", [])[:3])
        parts = []
        if modifiers:
            parts.append(f"modifiers={modifiers}")
        if extends_types:
            parts.append(f"extends={extends_types}")
        if implements_types:
            parts.append(f"implements={implements_types}")
        return "; ".join(parts) if parts else "type declaration header"

    def _build_method_block_summary(self, method_item: Dict[str, Any], type_name: str) -> str:
        signature = self._build_method_signature(method_item, type_name)
        conditions = method_item.get("conditions", [])
        called_methods = method_item.get("called_methods", [])
        instanceof_types = method_item.get("instanceof_types", [])
        cast_types = method_item.get("cast_types", [])
        kotlin_features = method_item.get("kotlin_features", [])
        summary_parts = [signature]
        if conditions:
            summary_parts.append(f"{len(conditions)} conditions")
        if called_methods:
            summary_parts.append(f"{len(called_methods)} calls")
        if instanceof_types:
            summary_parts.append(f"instanceof={', '.join(instanceof_types[:2])}")
        if cast_types:
            summary_parts.append(f"casts={', '.join(cast_types[:2])}")
        if kotlin_features:
            summary_parts.append(f"kotlin={', '.join(kotlin_features[:3])}")
        return "; ".join(summary_parts)

    def _classify_method_context_block(
        self,
        method_item: Dict[str, Any],
        language: str,
        default: str = "related_method",
    ) -> str:
        """Map raw AST method metadata to a precondition-oriented block role."""
        conditions = method_item.get("conditions", [])
        features = set(method_item.get("kotlin_features", []))
        called_methods = [str(name).lower() for name in method_item.get("called_methods", [])]
        condition_surface = " ".join(str(condition).lower() for condition in conditions)

        if "catch" in condition_surface or "exception" in condition_surface:
            return "exception_handling_block"
        if any(any(hint in name for hint in HELPER_NAME_HINTS) for name in called_methods):
            return "delegation_block"
        if any(feature in features for feature in ["safe_cast", "type_check", "when", "safe_call"]):
            return "guard_condition_block"
        if "elvis_return" in features or "return " in condition_surface:
            return "return_effect_block"
        if language == "kotlin" and method_item.get("is_extension"):
            return "extension_function"
        if language == "kotlin" and method_item.get("is_top_level"):
            return "top_level_function"
        if language == "kotlin" and method_item.get("kotlin_features"):
            return "kotlin_guard_function"
        return default

    def _find_semantic_line_clusters(
        self,
        path: str,
        start_line: int,
        end_line: int,
        block_patterns: Dict[str, List[str]],
    ) -> Dict[str, Tuple[int, int, str]]:
        try:
            lines = self._load_lines(path)
        except Exception:
            return {}

        scoped_lines = lines[max(0, start_line - 1):max(0, end_line)]
        if not scoped_lines:
            return {}

        clusters: Dict[str, Tuple[int, int, str]] = {}
        for block_kind, patterns in block_patterns.items():
            matching_indices: List[int] = []
            for offset, raw_line in enumerate(scoped_lines):
                line = raw_line.strip()
                if not line:
                    continue
                if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
                    matching_indices.append(offset)

            if not matching_indices:
                continue

            first = matching_indices[0]
            last = matching_indices[-1]
            cluster_start = start_line + max(0, first - 1)
            cluster_end = min(end_line, start_line + last + 2)
            preview_lines = scoped_lines[max(0, first - 1):min(len(scoped_lines), last + 2)]
            preview_text = "\n".join(preview_lines).strip()
            clusters[block_kind] = (cluster_start, cluster_end, preview_text)
        return clusters

    def _build_method_sub_block_hits(
        self,
        item: Dict[str, Any],
        path: str,
        method_item: Dict[str, Any],
        type_name: str,
        base_score: float,
    ) -> List[RetrievalHit]:
        start_line = int(method_item.get("start_line", 1))
        end_line = int(method_item.get("end_line", start_line))
        if end_line <= start_line:
            return []

        clusters = self._find_semantic_line_clusters(
            path=path,
            start_line=start_line,
            end_line=end_line,
            block_patterns=SUB_BLOCK_PATTERNS,
        )
        if not clusters:
            return []

        name = str(method_item.get("name", "")).strip()
        signature = self._build_method_signature(method_item, type_name)
        summary = self._build_method_block_summary(method_item, type_name)
        hits: List[RetrievalHit] = []
        block_bonus = {
            "guard_condition_block": 14.0,
            "delegation_block": 10.0,
            "exception_handling_block": 12.0,
            "return_effect_block": 8.0,
        }
        for block_kind, (cluster_start, cluster_end, preview_text) in clusters.items():
            overlap = self._score_reason_overlap_text(item, preview_text)
            if overlap == 0 and block_kind not in {"guard_condition_block", "delegation_block"}:
                continue
            semantic_block_type = {
                "guard_condition_block": "guard_condition_block",
                "delegation_block": "delegation_block",
                "exception_handling_block": "exception_handling_block",
                "return_effect_block": "return_effect_block",
            }.get(block_kind, block_kind)
            block_label = f"{type_name}.{name}() {block_kind.replace('_', ' ')}".strip()
            hits.append(
                self._make_ast_hit(
                    path=path,
                    start_line=cluster_start,
                    end_line=cluster_end,
                    score=150.0 + min(base_score, 16.0) + block_bonus.get(block_kind, 0.0) + overlap * 3.0,
                    text=f"{type_name}.{name}() {block_kind}",
                    node_kind="METHOD",
                    metadata={
                        "match_kind": "method_sub_block",
                        "block_kind": block_kind,
                        "semantic_block_type": semantic_block_type,
                        "block_label": block_label,
                        "reason_hint": "Focused method sub-block extracted from AST method body",
                        "summary": summary,
                        "signature": signature,
                        "return_type": method_item.get("return_type", ""),
                        "parameter_types": method_item.get("parameter_types", []),
                        "modifiers": method_item.get("modifiers", []),
                        "extension_receiver": method_item.get("extension_receiver", ""),
                        "kotlin_features": method_item.get("kotlin_features", []),
                        "conditions_count": len(method_item.get("conditions", [])),
                        "called_methods_count": len(method_item.get("called_methods", [])),
                        "reason_overlap_count": overlap,
                        "relevance_score": round(base_score + overlap * 2.0, 2),
                    },
                )
            )
        return hits

    def _build_type_context_hits(
        self,
        item: Dict[str, Any],
        path: str,
        parsed: Dict[str, Any],
        type_item: Dict[str, Any],
    ) -> List[RetrievalHit]:
        hits: List[RetrievalHit] = []
        type_name = type_item.get("name", "")
        start_line = int(type_item.get("start_line", 1))
        end_line = int(type_item.get("end_line", start_line))
        header_end_line = int(type_item.get("header_end_line", start_line))

        hits.append(
            self._make_ast_hit(
                path=path,
                start_line=max(1, start_line - 1),
                end_line=min(end_line, header_end_line + 2),
                score=220.0,
                text=f"{type_item.get('kind', 'TYPE')} {type_name} declaration",
                node_kind=type_item.get("kind", ""),
                metadata={
                    "match_kind": "type_context_block",
                    "block_kind": "type_header",
                    "semantic_block_type": "type_hierarchy_block",
                    "block_label": f"{type_name} declaration",
                    "reason_hint": "Type declaration and inheritance header",
                    "summary": self._build_type_header_summary(type_item),
                    "modifiers": type_item.get("modifiers", []),
                    "extends_types": type_item.get("extends_types", []),
                    "implements_types": type_item.get("implements_types", []),
                },
            )
        )

        methods = [
            method_item
            for method_item in parsed.get("methods", [])
            if method_item.get("owner_type") == type_name
        ]

        reason_text = str(item.get("reason", "")).lower()
        if any(token in reason_text for token in CONSTRUCTOR_REASON_HINTS):
            constructors = [
                method_item
                for method_item in methods
                if method_item.get("name") == "<init>"
            ]
            for constructor in constructors[:1]:
                c_start = int(constructor.get("start_line", start_line))
                c_end = int(constructor.get("end_line", c_start))
                hits.append(
                    self._make_ast_hit(
                        path=path,
                        start_line=c_start,
                        end_line=c_end,
                        score=185.0,
                        text=f"{type_name} constructor",
                        node_kind="METHOD",
                        metadata={
                            "match_kind": "type_context_block",
                            "block_kind": "constructor",
                            "semantic_block_type": "construction_block",
                            "block_label": f"{type_name} constructor",
                            "reason_hint": "Construction semantics may explain the type",
                            "summary": self._build_method_block_summary(constructor, type_name),
                            "signature": self._build_method_signature(constructor, type_name),
                            "parameter_types": constructor.get("parameter_types", []),
                            "modifiers": constructor.get("modifiers", []),
                        },
                    )
                )

        semantic_methods = sorted(
            [
                method_item
                for method_item in methods
                if method_item.get("name") != "<init>"
            ],
            key=lambda method_item: self._score_semantic_method_block(item, method_item, type_name),
            reverse=True,
        )

        added_method_names = set()
        for method_item in semantic_methods:
            name = method_item.get("name", "")
            if not name or name in added_method_names:
                continue
            if str(name).lower() in {"equals", "hashcode", "tostring"}:
                continue
            score = self._score_semantic_method_block(item, method_item, type_name)
            overlap_count = self._semantic_method_reason_overlap(item, method_item)
            if score < 10:
                continue
            if overlap_count == 0 and score < 16 and not type_name.lower().startswith("light"):
                continue
            added_method_names.add(name)
            m_start = int(method_item.get("start_line", start_line))
            m_end = int(method_item.get("end_line", m_start))
            language = method_item.get("language", self._source_language_for_path(path))
            block_kind = self._classify_method_context_block(
                method_item=method_item,
                language=language,
                default="related_method",
            )
            hits.append(
                self._make_ast_hit(
                    path=path,
                    start_line=m_start,
                    end_line=m_end,
                    score=165.0 + min(score, 18),
                    text=f"{type_name}.{name}() related method",
                    node_kind="METHOD",
                    metadata={
                        "match_kind": "type_context_block",
                        "block_kind": block_kind,
                        "semantic_block_type": block_kind,
                        "block_label": f"{type_name}.{name}()",
                        "reason_hint": "Related member that may explain the type semantics",
                        "summary": self._build_method_block_summary(method_item, type_name),
                        "signature": self._build_method_signature(method_item, type_name),
                        "return_type": method_item.get("return_type", ""),
                        "parameter_types": method_item.get("parameter_types", []),
                        "modifiers": method_item.get("modifiers", []),
                        "extension_receiver": method_item.get("extension_receiver", ""),
                        "kotlin_features": method_item.get("kotlin_features", []),
                        "conditions_count": len(method_item.get("conditions", [])),
                        "called_methods_count": len(method_item.get("called_methods", [])),
                        "reason_overlap_count": overlap_count,
                        "relevance_score": round(score, 2),
                    },
                )
            )
            hits.extend(
                self._build_method_sub_block_hits(
                    item=item,
                    path=path,
                    method_item=method_item,
                    type_name=type_name,
                    base_score=score,
                )
            )
            if len(added_method_names) >= 2:
                break

        for property_item in [
            property_item
            for property_item in parsed.get("properties", [])
            if property_item.get("owner_type") == type_name
        ][:2]:
            p_start = int(property_item.get("start_line", start_line))
            p_end = int(property_item.get("end_line", p_start))
            hits.append(
                self._make_ast_hit(
                    path=path,
                    start_line=p_start,
                    end_line=p_end,
                    score=155.0,
                    text=f"{type_name}.{property_item.get('name', '')} property",
                    node_kind="PROPERTY",
                    metadata={
                        "match_kind": "type_context_block",
                        "block_kind": "property_getter" if property_item.get("has_getter") else "property_declaration",
                        "semantic_block_type": "property_semantics_block",
                        "block_label": f"{type_name}.{property_item.get('name', '')}",
                        "reason_hint": "Kotlin property that may encode semantic state",
                        "summary": self._build_property_block_summary(property_item),
                        "return_type": property_item.get("return_type", ""),
                        "kotlin_features": property_item.get("kotlin_features", []),
                        "conditions_count": len(property_item.get("conditions", [])),
                        "called_methods_count": len(property_item.get("called_methods", [])),
                    },
                )
            )

        return hits

    def _build_property_block_summary(self, property_item: Dict[str, Any]) -> str:
        name = property_item.get("name", "")
        return_type = property_item.get("return_type", "")
        kind = property_item.get("kind", "val")
        summary = f"{kind} {name}"
        if return_type:
            summary += f": {return_type}"
        conditions = property_item.get("conditions", [])
        if conditions:
            summary += f"; {len(conditions)} conditions"
        kotlin_features = property_item.get("kotlin_features", [])
        if kotlin_features:
            summary += f"; kotlin={', '.join(kotlin_features[:3])}"
        return summary

    def _select_ast_context_range(
        self,
        path: str,
        symbol: str,
        kind: str,
        exact_start_line: int,
        exact_end_line: int,
        hit_metadata: Dict[str, Any],
    ) -> Tuple[int, int, str, Dict[str, Any]]:
        strategy = hit_metadata.get("block_kind", "exact_node") or "exact_node"
        details = {
            key: value
            for key, value in hit_metadata.items()
            if key not in {"match_kind", "reason_hint"}
        }
        return exact_start_line, exact_end_line, strategy, details

    def _summarize_ast_candidate(self, hit: RetrievalHit) -> Dict[str, Any]:
        metadata = hit.metadata or {}
        summary = str(metadata.get("summary", "")).strip()
        if not summary:
            summary = hit.text
        return {
            "path": hit.path,
            "line_range": f"{hit.line}-{hit.end_line or hit.line}",
            "language": metadata.get("language", self._source_language_for_path(hit.path)),
            "block_kind": metadata.get("block_kind", hit.node_kind or "node"),
            "semantic_block_type": metadata.get("semantic_block_type", metadata.get("block_kind", hit.node_kind or "node")),
            "block_label": metadata.get("block_label", hit.text),
            "summary": summary,
            "signature": metadata.get("signature", ""),
            "relevance_score": metadata.get("relevance_score", hit.score),
            "modifiers": metadata.get("modifiers", []),
            "extends_types": metadata.get("extends_types", []),
            "implements_types": metadata.get("implements_types", []),
            "parameter_types": metadata.get("parameter_types", []),
            "return_type": metadata.get("return_type", ""),
            "conditions_count": metadata.get("conditions_count", 0),
            "called_methods_count": metadata.get("called_methods_count", 0),
            "kotlin_features": metadata.get("kotlin_features", []),
            "extension_receiver": metadata.get("extension_receiver", ""),
        }

    def _fallback_select_ast_hits(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        limit = max(1, self.config.max_contexts_per_symbol)
        if not hits:
            return []

        type_header = next((hit for hit in hits if hit.metadata.get("block_kind") == "type_header"), None)
        constructors = sorted(
            [hit for hit in hits if hit.metadata.get("block_kind") == "constructor"],
            key=lambda hit: float(hit.score or 0.0),
            reverse=True,
        )
        semantic_methods = [
            hit
            for hit in hits
            if hit.metadata.get("block_kind") in SEMANTIC_CONTEXT_BLOCK_KINDS
        ]
        semantic_methods.sort(key=lambda hit: float(hit.score or 0.0), reverse=True)
        reason_text = str(item.get("reason", "")).lower()

        selected: List[RetrievalHit] = []
        if type_header is not None:
            selected.append(type_header)
        if len(selected) >= limit:
            return selected[:limit]

        if any(token in reason_text for token in CONSTRUCTION_REASON_HINTS):
            if constructors:
                selected.append(constructors[0])
            elif semantic_methods:
                selected.append(semantic_methods[0])
        elif semantic_methods:
            selected.append(semantic_methods[0])
        elif constructors:
            selected.append(constructors[0])

        if len(selected) < limit:
            for hit in hits:
                if hit in selected:
                    continue
                selected.append(hit)
                if len(selected) >= limit:
                    break
        return selected[:limit]

    def _plan_ast_contexts_with_llm(
        self,
        item: Dict[str, Any],
        hits: List[RetrievalHit],
    ) -> List[RetrievalHit]:
        if len(hits) <= 1:
            return hits

        started_at = time.time()
        limit = max(1, self.config.max_contexts_per_symbol)
        candidates = []
        planning_hits = hits[: max(1, int(self.config.max_ast_candidates_for_planning))]
        for idx, hit in enumerate(planning_hits):
            candidate = self._summarize_ast_candidate(hit)
            candidate["id"] = idx
            candidates.append(candidate)

        prompt = build_ast_context_planning_prompt(
            current_snippet=self._current_snippet,
            item=item,
            limit=limit,
            candidates=candidates,
        )

        fallback = self._fallback_select_ast_hits(item, planning_hits)
        fallback_ids = [planning_hits.index(hit) for hit in fallback if hit in planning_hits]
        result = self._llm_json(prompt, {"selected_ids": fallback_ids, "reason": "fallback"})
        selected_ids = result.get("selected_ids", fallback_ids)

        selected: List[RetrievalHit] = []
        for idx in selected_ids:
            if isinstance(idx, int) and 0 <= idx < len(planning_hits):
                candidate = planning_hits[idx]
                if candidate not in selected:
                    selected.append(candidate)
            if len(selected) >= limit:
                break

        if not selected:
            selected = fallback

        self._trace(
            "context_planning",
            {
                "symbol": item.get("symbol", ""),
                "candidates": len(candidates),
                "selected": len(selected),
                "selected_blocks": [hit.metadata.get("block_kind", "") for hit in selected],
                "languages": sorted({hit.metadata.get("language", self._source_language_for_path(hit.path)) for hit in selected}),
            },
            started_at,
        )
        return selected

