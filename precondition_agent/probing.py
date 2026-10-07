import re
import time

from typing import Any, Dict, List

from precondition_agent.language_rules.common_rules import (
    BOOLEAN_RETURN_PATTERNS,
    CONTROL_FLOW_CALL_NAMES,
    DIRECT_GUARD_TOKENS,
    HELPER_NAME_HINTS,
    JAVA_KOTLIN_KEYWORDS,
    NULL_GUARD_PATTERNS,
    ORDINARY_CONTAINER_TYPES,
    SEMANTIC_REASON_HINTS,
    SEMANTIC_TYPE_NAME_HINTS,
)
from precondition_agent.language_rules.java_rules import (
    JAVA_DECISIVE_CONDITION_PATTERNS,
    JAVA_GUARD_METHOD_HINTS,
    JAVA_LOW_VALUE_ANNOTATIONS,
    JAVA_LOW_VALUE_METHODS,
)
from precondition_agent.language_rules.kotlin_rules import (
    KOTLIN_DECISIVE_CONDITION_PATTERNS,
    KOTLIN_GUARD_METHOD_HINTS,
    LOW_VALUE_KOTLIN_METHODS,
    LOW_VALUE_KOTLIN_RECEIVERS,
    LOW_VALUE_KOTLIN_TYPES,
)
from precondition_agent.prompts import build_symbol_probe_prompt
from precondition_agent.utils import detect_kotlin_features, infer_entry_name, infer_snippet_language


class ProbingMixin:
    def probe_symbols(self, snippet: str, entry_name: str = "") -> Dict[str, Any]:
        started_at = time.time()
        effective_entry_name = entry_name or infer_entry_name(snippet)
        language = infer_snippet_language(snippet)

        decisive_conditions = self._extract_decisive_conditions(snippet, language)
        deterministic_slices = self._fallback_precondition_slices(snippet, decisive_conditions)
        candidate_symbols = self._prepare_unknown_symbol_candidates(
            snippet=snippet,
            entry_name=effective_entry_name,
            decisive_conditions=decisive_conditions,
            precondition_slices=deterministic_slices,
        )
        prompt = build_symbol_probe_prompt(
            snippet=snippet,
            effective_entry_name=effective_entry_name,
            decisive_conditions=decisive_conditions,
            candidate_symbols=[self._serialize_probe_candidate(item) for item in candidate_symbols],
        )
        fallback = {
            "_fallback_used": True,
            "decisive_conditions": decisive_conditions,
            "precondition_slices": deterministic_slices,
            "unknown_symbols": [
                self._fallback_unknown_symbol(item)
                for item in candidate_symbols[: self.config.max_symbol_candidates]
            ],
            "skip": [],
        }
        result = self._llm_json(prompt, fallback)
        result = self._normalize_probe_selection(
            result=result,
            snippet=snippet,
            entry_name=effective_entry_name,
            decisive_conditions=decisive_conditions,
            candidate_symbols=candidate_symbols,
        )
        if result.get("_fallback_used"):
            fallback["unknown_symbols"] = [
                self._fallback_unknown_symbol(item)
                for item in candidate_symbols[: self.config.max_symbol_candidates]
            ]
            result = self._normalize_probe_selection(
                result=fallback,
                snippet=snippet,
                entry_name=effective_entry_name,
                decisive_conditions=decisive_conditions,
                candidate_symbols=candidate_symbols,
            )
        result.pop("_fallback_used", None)

        self._trace(
            "boundary_detection",
            {
                "entry_name": effective_entry_name,
                "snippet_language": language,
                "kotlin_features": detect_kotlin_features(snippet),
                "precondition_slices_count": len(result.get("precondition_slices", [])),
                "candidate_symbols_count": len(result.get("candidate_symbols", [])),
                "unknown_symbols_count": len(result.get("unknown_symbols", [])),
                "roles": sorted(result.get("symbol_groups", {}).keys()),
                "decisive_conditions_count": len(result.get("decisive_conditions", [])),
            },
            started_at,
        )
        return result

    def _extract_decisive_conditions(self, snippet: str, language: str) -> List[str]:
        conditions = (
            self._extract_kotlin_decisive_conditions(snippet)
            if language == "kotlin"
            else self._extract_java_decisive_conditions(snippet)
        )
        normalized: List[str] = []
        for condition in conditions:
            compact = " ".join(str(condition).strip().split())
            if compact and compact not in normalized:
                normalized.append(compact)
        return self._prune_redundant_negated_conditions(normalized)[:12]

    def _prune_redundant_negated_conditions(self, conditions: List[str]) -> List[str]:
        negated_inners = set()
        for condition in conditions:
            stripped = condition.strip()
            paren_match = re.fullmatch(r"!\((.+)\)", stripped)
            if paren_match:
                inner = " ".join(paren_match.group(1).strip().split())
                if inner:
                    negated_inners.add(inner)
                continue
            simple_match = re.fullmatch(r"!\s*(.+)", stripped)
            if simple_match:
                inner = " ".join(simple_match.group(1).strip().split())
                if inner:
                    negated_inners.add(inner)

        pruned: List[str] = []
        for condition in conditions:
            if condition in negated_inners:
                continue
            pruned.append(condition)
        return pruned

    def _prepare_unknown_symbol_candidates(
        self,
        snippet: str,
        entry_name: str,
        decisive_conditions: List[str],
        precondition_slices: List[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        precondition_slices = precondition_slices or self._fallback_precondition_slices(snippet, decisive_conditions)
        deterministic_symbols = []
        deterministic_symbols.extend(
            self._extract_boundary_symbol_candidates(
                snippet=snippet,
                decisive_conditions=decisive_conditions,
                precondition_slices=precondition_slices,
            )
        )
        deterministic_symbols.extend(self._extract_deterministic_symbols(snippet, decisive_conditions))
        candidates = self._merge_symbol_candidates(
            llm_symbols=[],
            deterministic_symbols=deterministic_symbols,
            snippet=snippet,
            entry_name=entry_name,
            decisive_conditions=decisive_conditions,
        )
        if not candidates:
            candidates = self._merge_symbol_candidates(
                llm_symbols=[],
                deterministic_symbols=self._fallback_probe(snippet),
                snippet=snippet,
                entry_name=entry_name,
                decisive_conditions=decisive_conditions,
            )
        return candidates[: self.config.max_symbol_candidates]

    def _serialize_probe_candidate(self, item: Dict[str, Any]) -> Dict[str, Any]:
        serialized = {
            "symbol": item.get("symbol", ""),
            "kind": item.get("kind", ""),
            "role": item.get("role", ""),
            "priority": item.get("priority", 5),
            "reason": item.get("reason", ""),
            "region": item.get("region", "definition"),
            "query": item.get("query", ""),
        }
        if item.get("call_arity") is not None:
            serialized["call_arity"] = item.get("call_arity")
        return serialized

    def _fallback_unknown_symbol(self, item: Dict[str, Any]) -> Dict[str, Any]:
        preferred_context_map = {
            "delegated_checker": "method_definition",
            "direct_guard": "type_definition" if item.get("kind") == "type" else "method_definition",
            "exception_gate": "type_definition",
            "guard_value_producer": "method_definition",
            "state_signal": "property_definition",
            "supporting_type": "type_definition",
        }
        role = str(item.get("role", "supporting_type"))
        return {
            "symbol": item.get("symbol", ""),
            "priority": item.get("priority", 5),
            "why_unknown": item.get("reason", "") or "The snippet alone does not fully explain this symbol.",
            "why_it_matters": item.get("reason", "") or "Its semantics may change the precondition analysis.",
            "preferred_context": preferred_context_map.get(role, "method_definition"),
        }

    def _extract_boundary_symbol_candidates(
        self,
        snippet: str,
        decisive_conditions: List[str],
        precondition_slices: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        candidates.extend(self._extract_guard_value_producer_symbols(snippet, decisive_conditions))
        candidates.extend(self._extract_delegated_checker_symbols(snippet, precondition_slices))
        return candidates

    def _extract_guard_value_producer_symbols(
        self,
        snippet: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen = set()
        for guard_name in sorted(self._guard_value_names(decisive_conditions)):
            for symbol in self._initializer_calls_for_symbol(guard_name, snippet):
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                candidates.append(
                    {
                        "symbol": symbol,
                        "kind": "method",
                        "role": "guard_value_producer",
                        "priority": 1,
                        "top_k": 3,
                        "region": "definition",
                        "reason": f"Produces guard value `{guard_name}` used by a decisive condition.",
                    }
                )
        return candidates

    def _extract_delegated_checker_symbols(
        self,
        snippet: str,
        precondition_slices: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen = set()
        source_text = "\n".join(
            [snippet]
            + [
                str(item.get("code", ""))
                for item in precondition_slices
                if str(item.get("kind", "")) == "delegated_checker"
            ]
        )
        helper_hints = HELPER_NAME_HINTS | JAVA_GUARD_METHOD_HINTS | KOTLIN_GUARD_METHOD_HINTS
        for symbol in re.findall(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(", source_text):
            base_name = symbol.split(".")[-1]
            lowered = base_name.lower()
            if lowered in JAVA_KOTLIN_KEYWORDS or lowered in CONTROL_FLOW_CALL_NAMES:
                continue
            if base_name in JAVA_LOW_VALUE_METHODS or base_name in LOW_VALUE_KOTLIN_METHODS:
                continue
            if not self._base_name_has_helper_hint(base_name):
                continue
            qualified = self._qualify_method_symbol_from_snippet(symbol, snippet)
            if qualified in seen:
                continue
            seen.add(qualified)
            candidates.append(
                {
                    "symbol": qualified,
                    "kind": "method",
                    "role": "delegated_checker",
                    "priority": 1,
                    "top_k": 3,
                    "region": "definition",
                    "reason": "Delegated helper call likely contains refactoring precondition or conflict logic.",
                }
            )
        return candidates

    def _normalize_probe_selection(
        self,
        result: Dict[str, Any],
        snippet: str,
        entry_name: str,
        decisive_conditions: List[str],
        candidate_symbols: List[Dict[str, Any]] | None,
    ) -> Dict[str, Any]:
        result = dict(result or {})
        fallback_used = bool(result.pop("_fallback_used", False))
        precondition_slices = self._normalize_precondition_slices(
            result.get("precondition_slices", []),
            snippet=snippet,
            decisive_conditions=decisive_conditions,
        )
        selected_raw = result.get("unknown_symbols", [])
        if not isinstance(selected_raw, list):
            selected_raw = []

        candidate_symbols = candidate_symbols or []
        candidate_map = {str(item.get("symbol", "")).strip(): dict(item) for item in candidate_symbols}
        selected: List[Dict[str, Any]] = []
        seen = set()
        rejected_llm_symbols: List[Dict[str, str]] = []
        preferred_context_to_region = {
            "method_definition": "definition",
            "type_definition": "definition",
            "property_definition": "definition",
            "call_container": "method_body",
        }

        if candidate_map:
            for raw in selected_raw:
                if not isinstance(raw, dict):
                    continue
                raw_symbol = str(raw.get("symbol", "")).strip()
                symbol = self._qualify_method_symbol_from_snippet(raw_symbol, snippet)
                if not symbol:
                    continue
                if symbol not in candidate_map:
                    rejected_llm_symbols.append(
                        {
                            "symbol": raw_symbol,
                            "reason": "LLM-selected symbol is outside the deterministic candidate set.",
                        }
                    )
                    continue
                if symbol in seen:
                    continue
                merged = dict(candidate_map[symbol])
                merged["why_unknown"] = str(raw.get("why_unknown", "")).strip()
                merged["why_it_matters"] = str(raw.get("why_it_matters", "")).strip()
                merged["preferred_context"] = str(raw.get("preferred_context", "")).strip() or "method_definition"
                if raw.get("priority") is not None:
                    try:
                        merged["priority"] = min(int(merged.get("priority", 5)), int(raw.get("priority", 5)))
                    except Exception:
                        pass
                preferred_region = preferred_context_to_region.get(merged["preferred_context"], "")
                if preferred_region:
                    merged["region"] = preferred_region
                merged["reason"] = self._compose_unknown_symbol_reason(merged)
                merged["llm_selected"] = True
                merged["score"] = float(merged.get("score", 0.0)) + 5.0
                selected.append(merged)
                seen.add(symbol)
            for item in candidate_symbols:
                symbol = str(item.get("symbol", "")).strip()
                if not symbol or symbol in seen:
                    continue
                merged = dict(item)
                merged["why_unknown"] = str(merged.get("why_unknown", "") or merged.get("reason", "")).strip()
                merged["why_it_matters"] = str(merged.get("why_it_matters", "") or merged.get("reason", "")).strip()
                merged["preferred_context"] = str(merged.get("preferred_context", "")).strip() or (
                    "method_definition" if merged.get("kind") == "method" else "type_definition"
                )
                preferred_region = preferred_context_to_region.get(merged["preferred_context"], "")
                if preferred_region:
                    merged["region"] = preferred_region
                merged["reason"] = self._compose_unknown_symbol_reason(merged)
                merged["llm_selected"] = False
                selected.append(merged)
                seen.add(symbol)
        else:
            llm_candidates = self._normalize_llm_unknown_symbol_candidates(selected_raw, snippet)
            deterministic_candidates = self._extract_boundary_symbol_candidates(
                snippet=snippet,
                decisive_conditions=decisive_conditions,
                precondition_slices=precondition_slices,
            )
            selected = self._merge_symbol_candidates(
                llm_symbols=llm_candidates,
                deterministic_symbols=deterministic_candidates,
                snippet=snippet,
                entry_name=entry_name,
                decisive_conditions=decisive_conditions,
            )
            for item in selected:
                preferred_context = str(item.get("preferred_context", "")).strip() or (
                    "method_definition" if item.get("kind") == "method" else "type_definition"
                )
                item["preferred_context"] = preferred_context
                preferred_region = preferred_context_to_region.get(preferred_context, "")
                if preferred_region:
                    item["region"] = preferred_region

        if candidate_map and not selected:
            selected = [dict(item) for item in candidate_symbols[: self.config.max_symbol_candidates]]
            for item in selected:
                item["why_unknown"] = item.get("reason", "")
                item["why_it_matters"] = item.get("reason", "")
                item["preferred_context"] = "method_definition" if item.get("kind") == "method" else "type_definition"

        selected.sort(
            key=lambda item: (
                int(item.get("priority", 99)),
                -float(item.get("score", 0.0)),
                item.get("symbol", ""),
            )
        )

        normalized = {
            "_fallback_used": fallback_used,
            "decisive_conditions": decisive_conditions,
            "precondition_slices": precondition_slices,
            "unknown_symbols": selected[: self.config.max_symbol_candidates],
            "symbols": selected[: self.config.max_symbol_candidates],
            "candidate_symbols": [self._serialize_probe_candidate(item) for item in candidate_symbols],
            "skip": result.get("skip", []) if isinstance(result.get("skip", []), list) else [],
        }
        normalized["probe_audit"] = {
            "candidate_count": len(candidate_symbols),
            "kept_symbols": [
                {
                    "symbol": item.get("symbol", ""),
                    "role": item.get("role", ""),
                    "llm_selected": bool(item.get("llm_selected", False)),
                }
                for item in normalized["unknown_symbols"]
            ],
            "rejected_llm_symbols": rejected_llm_symbols,
        }
        normalized["symbol_groups"] = self._group_symbols_by_role(normalized["unknown_symbols"])
        return normalized

    def _normalize_precondition_slices(
        self,
        raw_slices: Any,
        snippet: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        allowed_kinds = {
            "direct_guard",
            "dependency_setup",
            "delegated_checker",
            "exception_gate",
            "state_signal",
            "supporting_context",
        }
        normalized: List[Dict[str, Any]] = []
        if isinstance(raw_slices, list):
            for raw in raw_slices:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get("code", "")).strip()
                reason = str(raw.get("reason", "")).strip()
                if not code:
                    continue
                kind = str(raw.get("kind", "supporting_context")).strip() or "supporting_context"
                if kind not in allowed_kinds:
                    kind = "supporting_context"
                condition = str(raw.get("condition", "")).strip()
                if condition.lower() in {"none", "n/a", "null"}:
                    condition = ""
                normalized.append(
                    {
                        "range": str(raw.get("range", "")).strip(),
                        "code": code,
                        "kind": kind,
                        "condition": condition,
                        "reason": reason,
                    }
                )
                if len(normalized) >= 12:
                    break

        return self._augment_precondition_slices(
            normalized or self._fallback_precondition_slices(snippet, decisive_conditions),
            snippet=snippet,
            decisive_conditions=decisive_conditions,
        )

    def _augment_precondition_slices(
        self,
        slices: List[Dict[str, Any]],
        snippet: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        augmented = list(slices)
        existing_codes = {str(item.get("code", "")).strip() for item in augmented}
        for fallback_slice in self._fallback_precondition_slices(snippet, decisive_conditions):
            code = str(fallback_slice.get("code", "")).strip()
            if not code or self._slice_code_already_covered(code, existing_codes):
                continue
            if fallback_slice.get("kind") not in {"direct_guard", "dependency_setup", "delegated_checker", "exception_gate"}:
                continue
            augmented.append(fallback_slice)
            existing_codes.add(code)
            if len(augmented) >= 12:
                break
        return augmented

    def _slice_code_already_covered(self, code: str, existing_codes: set) -> bool:
        normalized = " ".join(code.strip().split())
        if not normalized:
            return True
        for existing in existing_codes:
            existing_normalized = " ".join(str(existing).strip().split())
            if not existing_normalized:
                continue
            if normalized == existing_normalized:
                return True
            if normalized in existing_normalized:
                return True
            if existing_normalized in normalized and len(existing_normalized) >= 40:
                return True
        return False

    def _fallback_precondition_slices(
        self,
        snippet: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        slices: List[Dict[str, Any]] = []
        lines = snippet.splitlines()
        guard_variables = self._guard_value_names(decisive_conditions)

        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if any(condition and condition in stripped for condition in decisive_conditions):
                matched_condition = next((condition for condition in decisive_conditions if condition in stripped), "")
                if matched_condition == "return true" and re.fullmatch(r"return\s+true\s*;", stripped):
                    continue
                slices.append(
                    {
                        "range": f"L{index}",
                        "code": stripped,
                        "kind": "direct_guard",
                        "condition": matched_condition,
                        "reason": "Line contains a decisive guard condition.",
                    }
                )
                continue
            if any(re.search(rf"\b{re.escape(name)}\b\s*=", stripped) for name in guard_variables):
                slices.append(
                    {
                        "range": f"L{index}",
                        "code": stripped,
                        "kind": "dependency_setup",
                        "condition": "",
                        "reason": "Line assigns a value later used by a guard.",
                    }
                )
                continue
            if self._looks_like_method_declaration_line(stripped):
                continue
            if re.search(r"\b(check|validate|conflict|verify|can[A-Z]|is[A-Z])[A-Za-z0-9_]*\s*\(", stripped):
                slices.append(
                    {
                        "range": f"L{index}",
                        "code": stripped,
                        "kind": "delegated_checker",
                        "condition": "",
                        "reason": "Line calls a helper that may contain delegated precondition logic.",
                    }
                )
        return slices[:12]

    def _looks_like_method_declaration_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if re.search(r"\b(class|interface|enum|object)\s+[A-Za-z_][A-Za-z0-9_]*\b", stripped):
            return True
        if re.match(r"^(?:@\w+\s*)*(?:public|private|protected|internal|static|final|override|abstract|\s)+", stripped):
            return bool(re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)\s*(?:\{|throws|:)", stripped))
        if stripped.startswith("fun "):
            return True
        return False

    def _guard_value_names(self, decisive_conditions: List[str]) -> set:
        names = set()
        for condition in decisive_conditions:
            for pattern in [
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:==|!=)\s*null\b",
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s+instanceof\b",
                r"!\s*([A-Za-z_][A-Za-z0-9_.]*)\b",
                r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
            ]:
                for match in re.findall(pattern, condition):
                    names.add(str(match).split(".")[0])
        return names

    def _normalize_llm_unknown_symbol_candidates(
        self,
        raw_symbols: List[Any],
        snippet: str,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen = set()
        for raw in raw_symbols:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol", "")).strip()
            if not symbol:
                continue

            for candidate in self._expand_llm_unknown_symbol(raw, snippet):
                candidate_symbol = str(candidate.get("symbol", "")).strip()
                if not candidate_symbol or candidate_symbol in seen:
                    continue
                if not self._symbol_is_supported_by_snippet(candidate_symbol, snippet):
                    continue
                seen.add(candidate_symbol)
                candidates.append(candidate)
        return candidates

    def _expand_llm_unknown_symbol(self, raw: Dict[str, Any], snippet: str) -> List[Dict[str, Any]]:
        symbol = self._qualify_method_symbol_from_snippet(str(raw.get("symbol", "")).strip(), snippet)
        if not symbol:
            return []

        base_item = dict(raw)
        base_item["symbol"] = symbol
        base_item["kind"] = self._normalize_symbol_kind(str(base_item.get("kind", "")).strip().lower(), symbol)
        base_item["role"] = str(base_item.get("role", "")).strip().lower() or self._infer_symbol_role(symbol, base_item["kind"], snippet)
        base_item["priority"] = self._safe_priority(base_item.get("priority", 3))
        base_item["reason"] = self._compose_unknown_symbol_reason(base_item)
        base_item["preferred_context"] = str(base_item.get("preferred_context", "")).strip()
        items = [base_item]

        for initializer_call in self._initializer_calls_for_symbol(symbol, snippet):
            if initializer_call == symbol:
                continue
            promoted = dict(base_item)
            promoted["symbol"] = initializer_call
            promoted["kind"] = "method"
            promoted["role"] = "guard_value_producer"
            promoted["priority"] = min(int(base_item.get("priority", 3)), 2)
            promoted["preferred_context"] = "method_definition"
            promoted["reason"] = (
                f"{symbol} is a local guard value produced by {initializer_call}(). "
                f"{base_item.get('reason', '')}"
            ).strip()
            items.append(promoted)
        return items

    def _safe_priority(self, value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 3

    def _qualify_method_symbol_from_snippet(self, symbol: str, snippet: str) -> str:
        if "." in symbol or not symbol:
            return symbol
        qualified_match = re.search(rf"\b([A-Z][A-Za-z0-9_]*)\.{re.escape(symbol)}\s*\(", snippet)
        if qualified_match:
            return f"{qualified_match.group(1)}.{symbol}"
        return symbol

    def _symbol_is_supported_by_snippet(self, symbol: str, snippet: str) -> bool:
        if symbol in snippet:
            return True
        base_name = symbol.split(".")[-1]
        return bool(re.search(rf"\b{re.escape(base_name)}\b", snippet))

    def _initializer_calls_for_symbol(self, symbol: str, snippet: str) -> List[str]:
        if "." in symbol:
            return []
        calls: List[str] = []
        escaped = re.escape(symbol)
        patterns = [
            rf"\b(?:final\s+)?(?:@[A-Za-z_][A-Za-z0-9_.]*(?:\([^)]*\))?\s+)*(?:[A-Z][A-Za-z0-9_<>, ?@.\[\]]*\s+)+{escaped}\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
            rf"\b(?:val|var)\s+{escaped}(?:\s*:[^=]+)?\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, snippet):
                calls.append(self._qualify_method_symbol_from_snippet(str(match).strip(), snippet))
        return list(dict.fromkeys(calls))

    def _compose_unknown_symbol_reason(self, item: Dict[str, Any]) -> str:
        why_unknown = str(item.get("why_unknown", "")).strip()
        why_it_matters = str(item.get("why_it_matters", "")).strip()
        if why_unknown and why_it_matters:
            return f"{why_unknown} {why_it_matters}".strip()
        return why_unknown or why_it_matters or str(item.get("reason", "")).strip()

    def _merge_symbol_candidates(
        self,
        llm_symbols: List[Dict[str, Any]],
        deterministic_symbols: List[Dict[str, Any]],
        snippet: str,
        entry_name: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        ordered_candidates = []
        for source_name, symbols in [("llm", llm_symbols), ("deterministic", deterministic_symbols)]:
            if not isinstance(symbols, list):
                continue
            for item in symbols:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_probe_symbol(item, snippet, source_name)
                symbol = normalized.get("symbol", "")
                if not symbol:
                    continue
                existing = merged.get(symbol)
                if not existing:
                    merged[symbol] = normalized
                    ordered_candidates.append(symbol)
                    continue
                merged[symbol] = self._combine_symbol_metadata(existing, normalized)

        filtered = self._post_filter_symbols(
            symbols=[merged[symbol] for symbol in ordered_candidates],
            snippet=snippet,
            entry_name=entry_name,
            decisive_conditions=decisive_conditions,
        )
        filtered = self._drop_shadowed_method_symbols(filtered)
        filtered.sort(
            key=lambda item: (
                int(item.get("priority", 99)),
                -float(item.get("score", 0.0)),
                item.get("symbol", ""),
            )
        )
        return filtered

    def _normalize_probe_symbol(
        self,
        item: Dict[str, Any],
        snippet: str,
        source_name: str,
    ) -> Dict[str, Any]:
        normalized = dict(item)
        symbol = str(normalized.get("symbol", "")).strip()
        kind = self._normalize_symbol_kind(str(normalized.get("kind", "")).strip().lower(), symbol)
        role = str(normalized.get("role", "")).strip().lower() or self._infer_symbol_role(symbol, kind, snippet)
        priority = normalized.get("priority", 5)
        try:
            priority = int(priority)
        except Exception:
            priority = 5

        if source_name == "deterministic":
            priority = max(1, priority - 1)

        normalized["symbol"] = symbol
        normalized["kind"] = kind
        normalized["role"] = role
        normalized["priority"] = priority
        normalized["score"] = float(normalized.get("score", 0.0)) + self._score_symbol_candidate(symbol, kind, role, snippet)
        if kind == "method" and normalized.get("call_arity") is None:
            call_arity = self._call_argument_count_for_symbol(symbol, snippet)
            if call_arity is not None:
                normalized["call_arity"] = call_arity
        normalized.setdefault("top_k", 3)
        normalized.setdefault("region", "definition" if kind in {"type", "method"} else "method_body")
        normalized.setdefault("reason", self._default_reason_for_symbol(symbol, kind, role))
        return normalized

    def _combine_symbol_metadata(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        combined = dict(left)
        if int(right.get("priority", 99)) < int(left.get("priority", 99)):
            combined["priority"] = right.get("priority", left.get("priority", 5))
        combined["score"] = max(float(left.get("score", 0.0)), float(right.get("score", 0.0)))
        combined["top_k"] = max(int(left.get("top_k", 3)), int(right.get("top_k", 3)))
        if not combined.get("query") and right.get("query"):
            combined["query"] = right["query"]
        if combined.get("call_arity") is None and right.get("call_arity") is not None:
            combined["call_arity"] = right.get("call_arity")
        if len(str(right.get("reason", ""))) > len(str(left.get("reason", ""))):
            combined["reason"] = right.get("reason", left.get("reason", ""))
        if combined.get("role") in {"supporting_type", ""} and right.get("role"):
            combined["role"] = right["role"]
        return combined

    def _drop_shadowed_method_symbols(self, symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        qualified_methods = {
            str(item.get("symbol", "")).split(".")[-1]
            for item in symbols
            if str(item.get("kind", "")).lower() == "method" and "." in str(item.get("symbol", ""))
        }
        filtered: List[Dict[str, Any]] = []
        for item in symbols:
            symbol = str(item.get("symbol", "")).strip()
            kind = str(item.get("kind", "")).lower()
            if kind == "method" and "." not in symbol and symbol in qualified_methods:
                continue
            filtered.append(item)
        return filtered

    def _group_symbols_by_role(self, symbols: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for item in symbols:
            role = str(item.get("role", "supporting_type")).strip() or "supporting_type"
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            groups.setdefault(role, []).append(symbol)
        return groups

    def _post_filter_symbols(
        self,
        symbols: List[Dict[str, Any]],
        snippet: str,
        entry_name: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        """Apply a lightweight second-pass filter to reduce noisy boundary symbols."""
        snippet_language = infer_snippet_language(snippet)
        declared_param_names = self._extract_declared_parameter_names(snippet)
        declared_local_names = self._extract_declared_local_names(snippet)
        declared_param_types = self._extract_declared_parameter_types(snippet)
        declared_return_types = self._extract_declared_return_types(snippet)
        guard_symbols = self._extract_guard_symbols(snippet, decisive_conditions)
        direct_call_methods = self._extract_direct_call_methods(snippet)
        member_call_receivers = self._extract_member_call_receivers(snippet)
        decisive_text = snippet + "\n" + "\n".join(decisive_conditions or [])

        filtered: List[Dict[str, Any]] = []
        seen = set()

        for item in symbols:
            item = dict(item)
            symbol = (item.get("symbol") or "").strip()
            kind = (item.get("kind") or "").strip().lower()
            if not symbol:
                continue

            if symbol in seen:
                continue

            if entry_name and kind == "method" and symbol == entry_name:
                continue
            if symbol in declared_param_names or symbol in declared_local_names:
                continue
            if symbol.lstrip("@") in JAVA_LOW_VALUE_ANNOTATIONS:
                continue
            role = str(item.get("role", "")).strip().lower()
            if snippet_language == "kotlin" and self._is_low_value_kotlin_symbol(symbol, kind, snippet):
                continue
            if snippet_language == "java" and self._is_low_value_java_symbol(symbol, kind):
                continue
            if kind == "method" and self._is_ui_or_message_symbol(symbol):
                continue
            if kind == "method" and self._is_local_receiver_method_symbol(symbol):
                continue

            if kind in {"property"}:
                kind = "field"
                item["kind"] = "field"

            if kind == "field" and not self._is_explicit_state_signal(symbol, decisive_conditions):
                continue

            if kind in {"class", "type"}:
                if (
                    symbol in declared_param_types
                    and symbol not in guard_symbols
                    and not self._is_semantic_type_symbol(symbol, item, declared_return_types)
                ):
                    continue
                if symbol in self._ordinary_container_types():
                    continue

            if kind == "method":
                base_name = symbol.split(".")[-1]
                receivers = member_call_receivers.get(base_name, set())
                if "." not in symbol and base_name not in direct_call_methods and any(receiver[:1].islower() for receiver in receivers):
                    continue
                if not self._base_name_has_helper_hint(base_name) and role not in {"guard_value_producer", "exception_gate"}:
                    continue

            if kind == "class" and symbol and symbol[0].isupper():
                item["kind"] = "type"
                kind = "type"

            if kind in {"class", "type"}:
                item["query"] = rf"(?:interface|class|object)\s+{re.escape(symbol)}\b"
            elif kind == "method":
                if "." in symbol:
                    owner, method = symbol.rsplit(".", 1)
                    item["query"] = rf"(?:{re.escape(owner.split('.')[-1])}\.{re.escape(method)}\(|fun\s+.*\b{re.escape(method)}\s*\()"
                else:
                    item["query"] = rf"(?:{re.escape(symbol)}\s*\(|fun\s+.*\b{re.escape(symbol)}\s*\()"
            elif kind == "field":
                item["query"] = rf"(?:\b(?:val|var)\s+{re.escape(symbol)}\b|\b{re.escape(symbol)}\b)"

            if symbol not in guard_symbols and kind not in {"method", "type"} and role != "state_signal":
                continue

            seen.add(symbol)
            filtered.append(item)

        return filtered

    def _is_explicit_state_signal(self, symbol: str, decisive_conditions: List[str]) -> bool:
        if not symbol:
            return False
        condition_text = "\n".join(decisive_conditions or [])
        patterns = [
            rf"\b{re.escape(symbol)}\s*(?:==|!=)\s*null\b",
            rf"\b{re.escape(symbol)}\s+instanceof\b",
            rf"!\s*\(?\s*{re.escape(symbol)}\b",
            rf"\b{re.escape(symbol)}\s*\(",
            rf"\b{re.escape(symbol)}\.[A-Za-z_][A-Za-z0-9_]*\b",
        ]
        return any(re.search(pattern, condition_text) for pattern in patterns)

    def _base_name_has_helper_hint(self, base_name: str) -> bool:
        if not base_name:
            return False
        lowered = base_name.lower()
        prefix_hints = {
            "check",
            "validate",
            "verify",
            "should",
            "must",
            "find",
            "create",
            "build",
            "collect",
            "extract",
            "rename",
            "resolve",
            "try",
        }
        if any(lowered.startswith(prefix) for prefix in prefix_hints):
            return True
        if re.match(r"^(?:is|can)[A-Z]", base_name):
            return True
        semantic_hints = {
            "conflict",
            "available",
            "applicable",
            "recursive",
            "inline",
            "prototype",
            "specialization",
        }
        return any(token in lowered for token in semantic_hints)

    def _is_ui_or_message_symbol(self, symbol: str) -> bool:
        base_name = symbol.split(".")[-1]
        owner_name = symbol.rsplit(".", 1)[0].split(".")[-1] if "." in symbol else ""
        if owner_name in {
            "Messages",
            "CommonBundle",
            "JavaRefactoringBundle",
            "RefactoringBundle",
            "CommonRefactoringUtil",
            "DialogWrapper",
        }:
            return True
        if base_name in {
            "message",
            "getMessage",
            "getCancelButtonText",
            "getOkButtonText",
            "getQuestionIcon",
            "showOkCancelDialog",
            "showErrorHint",
            "showErrorDialog",
            "showHint",
            "getRefactoringName",
        }:
            return True
        return False

    def _is_local_receiver_method_symbol(self, symbol: str) -> bool:
        if "." not in symbol:
            return False
        receiver = symbol.split(".", 1)[0]
        return bool(receiver) and receiver[0].islower()

    def _call_argument_count_for_symbol(self, symbol: str, snippet: str) -> int | None:
        if not symbol:
            return None
        patterns = [re.escape(symbol)]
        base_name = symbol.split(".")[-1]
        if base_name != symbol:
            patterns.append(rf"(?:\.|(?<![\w.])){re.escape(base_name)}")
        for pattern in patterns:
            for match in re.finditer(rf"{pattern}\s*\(", snippet):
                open_index = snippet.find("(", match.start())
                if open_index < 0:
                    continue
                count = self._count_top_level_call_arguments(snippet, open_index)
                if count is not None:
                    return count
        return None

    def _count_top_level_call_arguments(self, text: str, open_index: int) -> int | None:
        depth = 0
        commas = 0
        has_content = False
        quote = ""
        escaped = False
        index = open_index + 1
        while index < len(text):
            ch = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                index += 1
                continue
            if ch in {"'", '"'}:
                quote = ch
                has_content = True
                index += 1
                continue
            if ch in "([{":
                depth += 1
                has_content = True
            elif ch in ")]}":
                if depth == 0:
                    return commas + 1 if has_content else 0
                depth -= 1
            elif ch == "," and depth == 0:
                commas += 1
            elif not ch.isspace():
                has_content = True
            index += 1
        return None

    def _normalize_symbol_kind(self, kind: str, symbol: str) -> str:
        if kind in {"class", "interface", "enum"}:
            return "type"
        if not kind:
            if symbol and symbol[0].isupper():
                return "type"
            if "." in symbol or symbol[:1].islower():
                return "method"
        return kind or "other"

    def _ordinary_container_types(self) -> set:
        return ORDINARY_CONTAINER_TYPES

    def _is_semantic_type_symbol(
        self,
        symbol: str,
        item: Dict[str, Any],
        declared_return_types: set,
    ) -> bool:
        reason = str(item.get("reason", "")).lower()
        if symbol in declared_return_types and any(token in symbol.lower() for token in SEMANTIC_TYPE_NAME_HINTS):
            return True
        if any(token in symbol.lower() for token in SEMANTIC_TYPE_NAME_HINTS):
            return True
        if any(token in reason for token in SEMANTIC_REASON_HINTS):
            return True
        return False

    def _is_low_value_java_symbol(self, symbol: str, kind: str) -> bool:
        base_name = symbol.split(".")[-1]
        if kind == "method" and base_name in JAVA_LOW_VALUE_METHODS:
            return True
        return False

    def _is_low_value_kotlin_symbol(self, symbol: str, kind: str, snippet: str) -> bool:
        base_name = symbol.split(".")[-1]
        if kind == "method" and base_name in LOW_VALUE_KOTLIN_METHODS:
            return True
        if kind in {"class", "type"} and symbol in LOW_VALUE_KOTLIN_TYPES:
            return True
        if "." in symbol and symbol.split(".", 1)[0] in LOW_VALUE_KOTLIN_RECEIVERS:
            return True
        return False

    def _infer_symbol_role(self, symbol: str, kind: str, snippet: str) -> str:
        lowered = snippet.lower()
        base_name = symbol.split(".")[-1].lower()
        if kind == "type":
            if re.search(rf"(instanceof\s+{re.escape(symbol)}\b|\bis\s+{re.escape(symbol)}\b|\b!is\s+{re.escape(symbol)}\b|\bas\??\s+{re.escape(symbol)}\b)", snippet):
                return "direct_guard"
            if "exception" in base_name or re.search(rf"catch\s*\([^)]*{re.escape(symbol)}", snippet):
                return "exception_gate"
            return "supporting_type"
        if kind == "field":
            return "state_signal"
        if kind == "method":
            helper_hints = HELPER_NAME_HINTS | JAVA_GUARD_METHOD_HINTS | KOTLIN_GUARD_METHOD_HINTS
            if any(base_name.startswith(prefix) or prefix in base_name for prefix in helper_hints):
                return "delegated_checker"
            if "exception" in base_name or "error" in base_name:
                return "exception_gate"
            if base_name in lowered and any(token in lowered for token in DIRECT_GUARD_TOKENS):
                return "direct_guard"
            return "delegated_checker"
        return "supporting_type"

    def _score_symbol_candidate(self, symbol: str, kind: str, role: str, snippet: str) -> float:
        score = 0.0
        base_name = symbol.split(".")[-1].lower()
        owner_name = symbol.rsplit(".", 1)[0].split(".")[-1] if "." in symbol else ""
        if role == "direct_guard":
            score += 25
        elif role == "delegated_checker":
            score += 18
        elif role == "guard_value_producer":
            score += 20
        elif role == "exception_gate":
            score += 14
        elif role == "state_signal":
            score += 10

        if kind == "type":
            score += 8
        if kind == "method" and "." in symbol:
            score += 6
            if owner_name[:1].isupper():
                score += 8
            else:
                score -= 18
        if kind == "method" and self._is_ui_or_message_symbol(symbol):
            score -= 40
        if any(base_name.startswith(prefix) or prefix in base_name for prefix in HELPER_NAME_HINTS):
            score += 6
        if "exception" in base_name or "conflict" in base_name:
            score += 7
        if symbol in snippet:
            score += 3
        return score

    def _default_reason_for_symbol(self, symbol: str, kind: str, role: str) -> str:
        if role == "direct_guard":
            return "Direct guard symbol used in a decisive type/null/boolean check."
        if role == "delegated_checker":
            return "Delegated helper likely containing the real precondition logic."
        if role == "guard_value_producer":
            return "Helper produces a value that is later checked by a decisive guard."
        if role == "exception_gate":
            return "Exception or failure control-flow symbol that changes whether the operation proceeds."
        if role == "state_signal":
            return "State-bearing property or field that may encode a precondition."
        if kind == "type":
            return "Supporting framework type needed to interpret the guard semantics."
        return "Semantic boundary symbol relevant to the precondition."

    def _extract_kotlin_decisive_conditions(self, snippet: str) -> List[str]:
        conditions: List[str] = []
        for pattern in KOTLIN_DECISIVE_CONDITION_PATTERNS:
            for match in re.findall(pattern, snippet):
                compact = " ".join(str(match).strip().split())
                if re.match(r"^[A-Z][A-Za-z0-9_.<>?]*\s*\?:\s*return\b", compact):
                    continue
                if compact and compact not in conditions:
                    conditions.append(compact)
        for condition in self._extract_common_decisive_conditions(snippet):
            if condition not in conditions:
                conditions.append(condition)
        return conditions[:12]

    def _extract_java_decisive_conditions(self, snippet: str) -> List[str]:
        conditions: List[str] = []
        for pattern in JAVA_DECISIVE_CONDITION_PATTERNS:
            for match in re.findall(pattern, snippet):
                compact = " ".join(str(match).strip().split())
                if compact and compact not in conditions:
                    conditions.append(compact)
        for condition in self._extract_common_decisive_conditions(snippet):
            if condition not in conditions:
                conditions.append(condition)
        return conditions[:12]

    def _extract_common_decisive_conditions(self, snippet: str) -> List[str]:
        conditions: List[str] = []
        for pattern in NULL_GUARD_PATTERNS + BOOLEAN_RETURN_PATTERNS:
            for match in re.findall(pattern, snippet):
                compact = " ".join(str(match).strip().split())
                if compact and compact not in conditions:
                    conditions.append(compact)
        return conditions[:8]

    def _extract_deterministic_symbols(
        self,
        snippet: str,
        decisive_conditions: List[str],
    ) -> List[Dict[str, Any]]:
        language = infer_snippet_language(snippet)
        symbols: List[Dict[str, Any]] = []
        symbols.extend(self._extract_type_guard_symbols(snippet))
        symbols.extend(self._extract_exception_symbols(snippet))
        symbols.extend(self._extract_guard_helper_symbols(snippet))
        if language == "kotlin":
            symbols.extend(self._extract_kotlin_property_symbols(snippet))
        else:
            symbols.extend(self._extract_java_field_symbols(snippet))
        return symbols

    def _extract_type_guard_symbols(self, snippet: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        for symbol in sorted(self._extract_guard_symbols(snippet, [])):
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            kind = "type" if symbol[:1].isupper() else "method"
            items.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "role": "direct_guard" if kind == "type" else "delegated_checker",
                    "priority": 1 if kind == "type" else 2,
                    "top_k": 3,
                    "region": "definition" if kind == "type" else "method_body",
                }
            )
        return items

    def _extract_exception_symbols(self, snippet: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        for symbol in re.findall(r"\bcatch\s*\([^)]*([A-Z][A-Za-z0-9_]*)\s+[A-Za-z_][A-Za-z0-9_]*\)", snippet):
            if symbol in seen:
                continue
            seen.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "kind": "type",
                    "role": "exception_gate",
                    "priority": 1,
                    "top_k": 2,
                    "region": "definition",
                    "reason": "Caught exception changes the control flow and therefore affects the effective precondition.",
                }
            )
        return items

    def _extract_guard_helper_symbols(self, snippet: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        helper_patterns = [
            r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
        ]
        for pattern in helper_patterns:
            for symbol in re.findall(pattern, snippet):
                base_name = symbol.split(".")[-1]
                lowered = base_name.lower()
                if lowered in JAVA_KOTLIN_KEYWORDS or lowered in CONTROL_FLOW_CALL_NAMES:
                    continue
                if base_name in seen:
                    continue
                if not any(token in lowered for token in HELPER_NAME_HINTS | JAVA_GUARD_METHOD_HINTS | KOTLIN_GUARD_METHOD_HINTS):
                    continue
                seen.add(base_name)
                items.append(
                    {
                        "symbol": symbol,
                        "kind": "method",
                        "role": "delegated_checker",
                        "priority": 2,
                        "top_k": 3,
                        "region": "definition",
                        "reason": "Helper call name suggests delegated precondition checking logic.",
                    }
                )
        return items

    def _extract_kotlin_property_symbols(self, snippet: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        for symbol in re.findall(r"(?:\.|\?\.)([a-z_][A-Za-z0-9_]*)\b(?!\s*\()", snippet):
            if symbol in {"message", "size", "length"} or symbol in seen:
                continue
            if symbol in LOW_VALUE_KOTLIN_METHODS:
                continue
            seen.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "kind": "field",
                    "role": "state_signal",
                    "priority": 3,
                    "top_k": 2,
                    "region": "definition",
                    "reason": "Property access may encode semantic state that influences the guard.",
                }
            )
        for symbol in re.findall(r"\b([a-z_][A-Za-z0-9_]*)\s*(?:\?:\s*return|==\s*null|!=\s*null)\b", snippet):
            if symbol in seen:
                continue
            seen.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "kind": "field",
                    "role": "state_signal",
                    "priority": 2,
                    "top_k": 2,
                    "region": "method_body",
                    "reason": "Nullability or Elvis-return check suggests this state value directly gates control flow.",
                }
            )
        return items

    def _extract_java_field_symbols(self, snippet: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        for symbol in re.findall(r"\b([a-z_][A-Za-z0-9_]*)\s*(?:==|!=)\s*null\b", snippet):
            if symbol in seen:
                continue
            seen.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "kind": "field",
                    "role": "state_signal",
                    "priority": 3,
                    "top_k": 2,
                    "region": "method_body",
                    "reason": "Null-checked state value may encode a direct precondition.",
                }
            )
        for symbol in re.findall(r"\breturn\s+([a-z_][A-Za-z0-9_]*)\s*;", snippet):
            if symbol in seen:
                continue
            seen.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "kind": "field",
                    "role": "state_signal",
                    "priority": 3,
                    "top_k": 2,
                    "region": "method_body",
                    "reason": "Returned boolean/state flag may encode the acceptance decision.",
                }
            )
        return items

    def _extract_declared_parameter_types(self, snippet: str) -> set:
        match = re.search(r"\((.*?)\)", snippet, flags=re.DOTALL)
        if not match:
            return set()

        param_text = match.group(1)
        tokens = re.findall(r":\s*([A-Z][A-Za-z0-9_]*(?:<[^>]+>)?)", param_text)
        if not tokens:
            tokens = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", param_text)
        normalized = []
        for token in tokens:
            normalized.extend(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", token))
        return set(normalized)

    def _extract_declared_return_types(self, snippet: str) -> set:
        return_types = set()
        for match in re.findall(r"\)\s*:\s*([A-Z][A-Za-z0-9_]*(?:<[^>{}]+>)?)", snippet):
            return_types.update(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", match))
        return return_types

    def _extract_declared_parameter_names(self, snippet: str) -> set:
        match = re.search(r"\((.*?)\)", snippet, flags=re.DOTALL)
        if not match:
            return set()

        names = set()
        param_text = match.group(1)
        for part in param_text.split(","):
            part = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part).strip()
            kotlin_match = re.match(r"(?:vararg\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:", part)
            if kotlin_match:
                candidate = kotlin_match.group(1)
            else:
                tokens = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", part)
                candidate = tokens[-1] if tokens else ""
            if candidate:
                if candidate not in {"final", "public", "private", "protected"}:
                    names.add(candidate)
        return names

    def _extract_declared_local_names(self, snippet: str) -> set:
        names = set()
        java_pattern = re.compile(
            r"(?:^|[;{}]\s*)(?:final\s+)?(?:@[A-Za-z_][A-Za-z0-9_.]*(?:\([^)]*\))?\s+)*(?:[A-Z][A-Za-z0-9_<>, ?@.\[\]]*\s+)+([a-z_][A-Za-z0-9_]*)\s*=",
            flags=re.MULTILINE,
        )
        for match in java_pattern.finditer(snippet):
            names.add(match.group(1))
        kotlin_pattern = re.compile(r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
        for match in kotlin_pattern.finditer(snippet):
            names.add(match.group(1))
        return names

    def _extract_direct_call_methods(self, snippet: str) -> set:
        methods = set(re.findall(r"(?<![\w.])\b([a-z_][A-Za-z0-9_]*)\s*\(", snippet))
        methods |= set(re.findall(r"(?:\.|\?\.)([a-z_][A-Za-z0-9_]*)\s*\(", snippet))
        methods -= CONTROL_FLOW_CALL_NAMES
        return methods

    def _extract_member_call_receivers(self, snippet: str) -> Dict[str, set]:
        receiver_map: Dict[str, set] = {}
        for receiver, method in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\.|\?\.)\s*([a-z_][A-Za-z0-9_]*)\s*\(", snippet):
            receiver_map.setdefault(method, set()).add(receiver)
        return receiver_map

    def _extract_guard_symbols(self, snippet: str, decisive_conditions: List[str]) -> set:
        text = snippet + "\n" + "\n".join(decisive_conditions or [])
        symbols = set(re.findall(r"instanceof\s+([A-Z][A-Za-z0-9_]*)", text))
        symbols |= set(re.findall(r"(?:\bis\b|!is\s+|\bas\?\s+|\bas\s+)([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*)", text))
        symbols = {symbol.split(".")[-1] for symbol in symbols}
        constants = set(re.findall(r"\b([A-Z][A-Za-z0-9_]*\.[A-Z][A-Z0-9_]*)\b", text))
        method_calls = set(re.findall(r"\b([a-z_][A-Za-z0-9_]*)\s*\(", text))
        properties = set(re.findall(r"(?:\.|\?\.)([a-z_][A-Za-z0-9_]*)\b(?!\s*\()", text))
        method_calls -= CONTROL_FLOW_CALL_NAMES
        return symbols | constants | method_calls | properties

    def _fallback_probe(self, snippet: str) -> List[Dict[str, Any]]:
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", snippet)
        class_like = sorted({t for t in tokens if t and t[0].isupper()})
        kotlin_cast_types = sorted(
            {
                type_name.split(".")[-1]
                for type_name in re.findall(
                    r"(?:\bis\b|!is\s+|\bas\?\s+|\bas\s+)([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*)",
                    snippet,
                )
            }
        )
        class_like = sorted((set(class_like) | set(kotlin_cast_types)) - ORDINARY_CONTAINER_TYPES)
        method_like = sorted(self._extract_direct_call_methods(snippet))
        qualified_method_calls = sorted(
            {
                f"{receiver}.{method}"
                for receiver, method in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*(?:\.|\?\.)\s*([a-z_][A-Za-z0-9_]*)\s*\(", snippet)
            }
        )
        kotlin_properties = sorted(
            {
                name
                for name in re.findall(r"(?:\.|\?\.)([a-z_][A-Za-z0-9_]*)\b(?!\s*\()", snippet)
                if name not in {"message", "size", "length"}
            }
        )

        method_like = [m for m in method_like if m.lower() not in JAVA_KOTLIN_KEYWORDS]

        items: List[Dict[str, Any]] = []

        for name in class_like[:4]:
            items.append(
                {
                    "symbol": name,
                    "kind": "type",
                    "role": "direct_guard",
                    "query": rf"(?:interface|class|object)\s+{re.escape(name)}\b",
                    "priority": 3,
                    "top_k": 2,
                    "region": "definition",
                    "reason": "Referenced type or Kotlin type guard may affect refactoring semantics.",
                }
            )

        for name in method_like[:4]:
            items.append(
                {
                    "symbol": name,
                    "kind": "method",
                    "role": "delegated_checker",
                    "query": rf"(?:\b{re.escape(name)}\s*\(|\bfun\s+.*\b{re.escape(name)}\s*\()",
                    "priority": 4,
                    "top_k": 3,
                    "region": "method_body",
                    "reason": "Method call may affect a decisive condition.",
                }
            )

        for name in qualified_method_calls[:2]:
            items.append(
                {
                    "symbol": name,
                    "kind": "method",
                    "role": "delegated_checker",
                    "query": rf"{re.escape(name)}\s*\(",
                    "priority": 5,
                    "top_k": 2,
                    "region": "definition",
                    "reason": "Qualified helper call may affect a decisive condition.",
                }
            )

        for name in kotlin_properties[:3]:
            items.append(
                {
                    "symbol": name,
                    "kind": "property",
                    "role": "state_signal",
                    "query": rf"(?:\b(?:val|var)\s+{re.escape(name)}\b|\b{re.escape(name)}\b)",
                    "priority": 5,
                    "top_k": 2,
                    "region": "definition",
                    "reason": "Kotlin property access may encode a semantic precondition.",
                }
            )

        return items[: self.config.max_symbol_candidates]
