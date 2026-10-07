import re
import time

from typing import Any, Dict, List, Optional

from precondition_agent.language_rules.synthesis_rules import (
    BANNED_DECISIVE_CONDITION_PATTERNS,
    CORE_ANALYSIS_TOKENS,
    ENVIRONMENT_UI_TOKENS,
    ERROR_MESSAGE_ONLY_TOKENS,
    NON_PRECONDITION_RESULT_TOKENS,
    UNSUPPORTED_DEEP_DELEGATED_TOKENS,
)
from precondition_agent.prompts import build_compare_engines_prompt, build_synthesis_prompt
from precondition_agent.schemas import GroundedSymbol, RetrievedContext
from precondition_agent.utils import normalize_text_key


class SynthesisMixin:
    def synthesize(
        self,
        engine: str,
        snippet: str,
        probe_result: Dict[str, Any],
        contexts: List[RetrievedContext],
        grounded_symbols: List[GroundedSymbol],
        entry_name: str = "",
    ) -> Dict[str, Any]:
        started_at = time.time()

        context_payload = []
        for idx, ctx in enumerate(contexts, start=1):
            context_payload.append(
                {
                    "context_id": f"ctx_{idx}",
                    "symbol": ctx.symbol,
                    "kind": ctx.kind,
                    "source": ctx.source,
                    "language": self._source_language_for_path(ctx.path),
                    "path": ctx.path,
                    "anchor_line": ctx.anchor_line,
                    "range": f"{ctx.start_line}-{ctx.end_line}",
                    "reason": ctx.reason,
                    "code": ctx.code,
                }
            )

        grounded_payload = [self._serialize_grounded_symbol(item) for item in grounded_symbols]

        prompt = build_synthesis_prompt(
            engine=engine,
            entry_name=entry_name,
            snippet=snippet,
            probe_result=probe_result,
            grounded_payload=grounded_payload,
            context_payload=context_payload,
        )

        fallback = {
            "engine": engine,
            "entry_name": entry_name,
            "rule_name": entry_name or "unknown_rule",
            "constraints": [],
            "direct_preconditions": [],
            "delegated_conflict_rules": [],
            "decisive_conditions": probe_result.get("decisive_conditions", []),
            "unknown_symbols": [s.get("symbol", "") for s in probe_result.get("symbols", [])[:3]],
            "notes": "Fallback result because synthesis JSON parsing failed.",
        }
        result = self._normalize_analysis_output(
            self._llm_json(prompt, fallback),
            grounded_symbols,
        )

        self._trace(
            "precondition_synthesis",
            {
                "engine": engine,
                "constraints_count": len(result.get("constraints", [])),
                "direct_preconditions_count": len(result.get("direct_preconditions", [])),
                "delegated_conflict_rules_count": len(result.get("delegated_conflict_rules", [])),
                "unknown_symbols_count": len(result.get("unknown_symbols", [])),
            },
            started_at,
        )
        return result

    def _normalize_constraint_category(self, raw: str, item: Dict[str, Any]) -> str:
        value = str(raw or "").strip().lower()
        mapping = {
            "direct_precondition": "direct_precondition",
            "direct": "direct_precondition",
            "precondition": "direct_precondition",
            "delegated_conflict_rule": "delegated_conflict_rule",
            "delegated": "delegated_conflict_rule",
            "conflict_rule": "delegated_conflict_rule",
            "supporting_fact": "supporting_fact",
            "support": "supporting_fact",
            "fact": "supporting_fact",
        }
        if value in mapping:
            return mapping[value]

        condition = str(item.get("condition", "")).lower()
        error_message = str(item.get("error_message", "")).lower()
        depends_on = " ".join(str(x).lower() for x in item.get("depends_on", []) if isinstance(x, str))
        evidence = " ".join(str(x).lower() for x in item.get("evidence", []) if isinstance(x, str))
        joined = " ".join([condition, error_message, depends_on, evidence])
        if any(token in condition for token in ["returns null", " is null", " == null", "cannot be created", "early return", "?: return", "as?"]):
            return "direct_precondition"
        if any(token in joined for token in ["conflictsutil", "already exists", "override", "hide method", "defined in the class"]):
            return "delegated_conflict_rule"
        return "direct_precondition"

    def _normalize_constraint_polarity(self, raw: str, condition: str, semantic_condition: str) -> str:
        value = str(raw or "").strip().lower()
        mapping = {
            "required": "required",
            "positive": "required",
            "blocking": "blocking",
            "block": "blocking",
            "conflict": "conflict",
            "warning": "conflict",
        }
        if value in mapping:
            return mapping[value]

        text = f"{condition} {semantic_condition}".lower()
        if any(token in text for token in ["returns null", " is null", " == null", "cannot ", "fails to ", "early return", "?: return", "as?"]):
            return "blocking"
        if any(token in text for token in ["conflict", "already exists", "override", "hide ", "defined in the class"]):
            return "conflict"
        return "required"

    def _normalize_required_condition(
        self,
        raw_normalized_condition: str,
        condition: str,
        semantic_condition: str,
        polarity: str,
        error_message: str,
    ) -> str:
        normalized = raw_normalized_condition.strip() or condition.strip()
        if not normalized and not semantic_condition.strip():
            return ""
        if polarity != "blocking":
            return self._canonicalize_requirement_text(normalized)

        for candidate in [
            raw_normalized_condition.strip(),
            semantic_condition.strip(),
            condition.strip(),
        ]:
            rewritten = self._rewrite_blocking_condition_to_requirement(candidate)
            if rewritten:
                return rewritten

        if error_message and "early return" in error_message.lower():
            return f"NOT ({normalized})"
        return self._canonicalize_requirement_text(normalized)

    def _canonicalize_requirement_text(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip())
        if not normalized:
            return normalized

        canonical_patterns = [
            (r"^\s*(.+?)\s*!=\s*null\s*$", r"\1 is non-null"),
            (r"^\s*(.+?)\s+returns\s+non-null\s*$", r"\1 returns non-null"),
        ]
        for pattern, replacement in canonical_patterns:
            updated = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
            if updated != normalized:
                return re.sub(r"\s+", " ", updated).strip()
        return normalized

    def _rewrite_blocking_condition_to_requirement(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip())
        if not normalized:
            return ""

        rewrite_patterns = [
            (r"^\s*(.+?)\s+is\s+not\s+non-null\s*$", r"\1 is non-null"),
            (r"^\s*(.+?)\s*==\s*null\s*$", r"\1 is non-null"),
            (r"^\s*(.+?)\s+is\s+null\s*$", r"\1 is non-null"),
            (r"^\s*(.+?)\s+returns\s+null\s*$", r"\1 returns non-null"),
            (r"^\s*(.+?)\s+cannot\s+be\s+(created|constructed|built|resolved|determined)\s*$", r"\1 can be \2"),
            (r"^\s*(.+?)\s+could\s+not\s+be\s+(created|constructed|built|resolved|determined)\s*$", r"\1 can be \2"),
            (r"^\s*(.+?)\s+cannot\s+be\s+safely\s+cast(?:\s+as\s+(.+))?\s*$", r"\1 can be safely cast as \2"),
            (r"^\s*(.+?)\s+must\s+not\s+be\s+null\s*$", r"\1 is non-null"),
        ]
        for pattern, replacement in rewrite_patterns:
            updated = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
            if updated != normalized:
                updated = re.sub(r"\s+", " ", updated).strip()
                updated = re.sub(r"\s+as\s+$", "", updated).strip()
                return self._canonicalize_requirement_text(updated)

        if "not non-null" in normalized.lower():
            updated = re.sub(r"\bnot\s+non-null\b", "non-null", normalized, flags=re.IGNORECASE)
            return self._canonicalize_requirement_text(updated)

        return ""

    def _apply_grounded_meanings(
        self,
        text: str,
        grounded_map: Dict[str, str],
    ) -> str:
        updated = text
        for symbol in sorted(grounded_map.keys(), key=len, reverse=True):
            meaning = grounded_map[symbol].strip()
            if not meaning:
                continue
            updated = re.sub(rf"\b{re.escape(symbol)}\b", meaning, updated)
        updated = re.sub(
            r"\b(a|an)\s+(a|an)\s+",
            lambda match: f"{match.group(1)} ",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(r"\s+", " ", updated).strip()
        return updated

    def _constraint_comparison_key(
        self,
        category: str,
        polarity: str,
        normalized_required_condition: str,
        semantic_condition: str,
    ) -> str:
        comparable_condition = semantic_condition or normalized_required_condition
        payload = " | ".join(
            [
                category.strip().lower(),
                polarity.strip().lower(),
                normalize_text_key(comparable_condition),
            ]
        )
        return normalize_text_key(payload)

    def _constraint_support_level(self, evidence: List[str], depends_on: List[str]) -> str:
        evidence_count = len([item for item in evidence if str(item).strip()])
        depends_count = len([item for item in depends_on if str(item).strip()])
        if evidence_count >= 2 or (evidence_count >= 1 and depends_count >= 2):
            return "high"
        if evidence_count >= 1:
            return "medium"
        return "low"

    def _normalize_analysis_output(
        self,
        result: Dict[str, Any],
        grounded_symbols: Optional[List[GroundedSymbol]] = None,
    ) -> Dict[str, Any]:
        constraints = result.get("constraints", [])
        normalized_constraints: List[Dict[str, Any]] = []
        deduped_constraints: Dict[str, Dict[str, Any]] = {}
        grounded_map = {
            item.symbol: item.grounded_meaning
            for item in (grounded_symbols or [])
            if item.grounded_meaning and item.grounded_meaning.lower() != item.symbol.lower()
        }

        if not isinstance(constraints, list):
            constraints = []

        for item in constraints:
            if not isinstance(item, dict):
                continue

            condition = str(item.get("condition", "")).strip()
            semantic_condition = str(item.get("semantic_condition", "")).strip() or condition
            category = self._normalize_constraint_category(item.get("category", ""), item)
            polarity = self._normalize_constraint_polarity(item.get("polarity", ""), condition, semantic_condition)
            if category == "delegated_conflict_rule" and polarity == "required":
                polarity = "conflict"
            if self._should_drop_over_inferred_constraint(item, condition, semantic_condition):
                continue
            item["category"] = category
            item["polarity"] = polarity
            normalized_required_condition = self._normalize_required_condition(
                raw_normalized_condition=str(item.get("normalized_required_condition", "")).strip(),
                condition=condition,
                semantic_condition=semantic_condition,
                polarity=polarity,
                error_message=str(item.get("error_message", "")).strip(),
            )

            semantic_condition = self._apply_grounded_meanings(semantic_condition, grounded_map)
            normalized_required_condition = self._apply_grounded_meanings(normalized_required_condition, grounded_map)
            evidence = item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []
            depends_on = item.get("depends_on", []) if isinstance(item.get("depends_on", []), list) else []
            comparison_key = self._constraint_comparison_key(
                category=category,
                polarity=polarity,
                normalized_required_condition=normalized_required_condition,
                semantic_condition=semantic_condition,
            )
            normalized_item = {
                "condition": condition,
                "semantic_condition": semantic_condition,
                "error_message": str(item.get("error_message", "")).strip(),
                "level": str(item.get("level", "WARNING")).strip() or "WARNING",
                "category": category,
                "polarity": polarity,
                "normalized_required_condition": normalized_required_condition,
                "comparison_key": comparison_key,
                "support_level": self._constraint_support_level(evidence, depends_on),
                "evidence": evidence,
                "depends_on": depends_on,
            }
            existing = deduped_constraints.get(comparison_key)
            if not existing:
                deduped_constraints[comparison_key] = normalized_item
                continue

            merged_evidence = list(dict.fromkeys(existing.get("evidence", []) + evidence))
            merged_depends = list(dict.fromkeys(existing.get("depends_on", []) + depends_on))
            if len(semantic_condition) > len(existing.get("semantic_condition", "")):
                existing["semantic_condition"] = semantic_condition
            if len(condition) > len(existing.get("condition", "")):
                existing["condition"] = condition
            if len(normalized_required_condition) > len(existing.get("normalized_required_condition", "")):
                existing["normalized_required_condition"] = normalized_required_condition
            existing["evidence"] = merged_evidence
            existing["depends_on"] = merged_depends
            existing["support_level"] = self._constraint_support_level(merged_evidence, merged_depends)
            if existing.get("level", "WARNING") == "WARNING" and normalized_item["level"] == "FATAL":
                existing["level"] = "FATAL"
            if not existing.get("error_message") and normalized_item.get("error_message"):
                existing["error_message"] = normalized_item["error_message"]

        normalized_constraints = list(deduped_constraints.values())
        result["constraints"] = normalized_constraints
        result["decisive_conditions"] = self._normalize_decisive_conditions(
            result.get("decisive_conditions", []),
            normalized_constraints,
        )
        result["direct_preconditions"] = [
            item for item in normalized_constraints if item.get("category") == "direct_precondition"
        ]
        result["delegated_conflict_rules"] = [
            item for item in normalized_constraints if item.get("category") == "delegated_conflict_rule"
        ]
        return result

    def _normalize_decisive_conditions(
        self,
        decisive_conditions: Any,
        constraints: List[Dict[str, Any]],
    ) -> List[str]:
        if not isinstance(decisive_conditions, list):
            decisive_conditions = []

        normalized: List[str] = []
        for raw in decisive_conditions:
            text = str(raw).strip()
            if not text:
                continue
            lowered = text.lower()
            if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in BANNED_DECISIVE_CONDITION_PATTERNS):
                continue
            if text not in normalized:
                normalized.append(text)

        for item in constraints:
            condition = str(item.get("condition", "")).strip()
            if condition and condition not in normalized:
                normalized.append(condition)

        return normalized[:8]

    def _should_drop_over_inferred_constraint(
        self,
        item: Dict[str, Any],
        condition: str,
        semantic_condition: str,
    ) -> bool:
        """Drop common LLM over-inferences that are not real refactoring preconditions."""
        text = " ".join(
            [
                condition,
                semantic_condition,
                str(item.get("error_message", "")),
                " ".join(str(dep) for dep in item.get("depends_on", []) if isinstance(dep, str)),
            ]
        ).lower()

        # Returning an empty list on failure does not imply the successful result must be non-empty.
        if any(token in text for token in NON_PRECONDITION_RESULT_TOKENS):
            return True

        # Project/editor/progress/read-action wrappers are execution environment facts unless explicitly checked.
        if any(token in text for token in ENVIRONMENT_UI_TOKENS) and not any(token in text for token in CORE_ANALYSIS_TOKENS):
            return True

        # Error-message completeness is useful for UI display but not a precondition for refactoring feasibility.
        if any(token in text for token in ERROR_MESSAGE_ONLY_TOKENS):
            return True

        if self._is_unsupported_deep_delegated_detail(item, text):
            return True

        if self._is_setup_step_constraint(item, condition, semantic_condition):
            return True

        return False

    def _is_setup_step_constraint(
        self,
        item: Dict[str, Any],
        condition: str,
        semantic_condition: str,
    ) -> bool:
        stripped = condition.strip()
        if not stripped:
            return False

        text = " ".join(
            [
                stripped,
                semantic_condition,
                str(item.get("error_message", "")),
            ]
        ).lower()
        if "setup step" in text or "setup" in text or "n/a" in text:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.\[\]]*\s*=\s*[^=].*$", stripped):
                return True

        if re.match(r"^[A-Za-z_][A-Za-z0-9_.\[\]]*\s*=\s*[^=].*$", stripped):
            if any(token in text for token in ["assigned", "set to", "before conflict checking", "before conflict check"]):
                return True

        return False

    def _is_unsupported_deep_delegated_detail(self, item: Dict[str, Any], text: str) -> bool:
        category = str(item.get("category", "")).lower()
        if category != "delegated_conflict_rule":
            return False

        if not any(token in text for token in UNSUPPORTED_DEEP_DELEGATED_TOKENS):
            return False

        evidence = item.get("evidence", [])
        evidence_text = " ".join(str(value) for value in evidence if isinstance(value, str)).lower()
        # If the model only cites a tiny delegated context range, collapse the detail rather than trusting it.
        if "ctx_" in evidence_text and not any(token in evidence_text for token in ["l1-", "l20-", "l30-", "l40-"]):
            return True
        return True

    # -------------------------
    # Pipeline
    # -------------------------

    def compare_engines(self, analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
        started_at = time.time()
        prompt = build_compare_engines_prompt(analyses)

        fallback = {
            "common_constraints": [],
            "engine_specific": [],
            "inconsistencies": [],
            "suggested_test_focus": [],
        }
        result = self._llm_json(prompt, fallback)
        self._trace(
            "compare_engines",
            {
                "analyses_count": len(analyses),
                "inconsistencies_count": len(result.get("inconsistencies", [])),
            },
            started_at,
        )
        return result

