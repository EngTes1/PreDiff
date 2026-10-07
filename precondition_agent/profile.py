import re

from typing import Any, Dict, List, Tuple

from precondition_agent.utils import normalize_text_key


def build_unit_profile(
    unit: Dict[str, Any],
    analysis_result: Dict[str, Any],
    strict: bool = True,
) -> Dict[str, Any]:
    """Build a strict, comparison-oriented profile for one analyzed corpus unit."""
    raw_constraints = analysis_result.get("analysis", {}).get("constraints", [])
    constraints: List[Dict[str, Any]] = []
    dropped_claims: List[Dict[str, Any]] = []
    uncertain_constraints: List[Dict[str, Any]] = []

    if not isinstance(raw_constraints, list):
        raw_constraints = []

    for index, raw in enumerate(raw_constraints, start=1):
        if not isinstance(raw, dict):
            continue

        ok, reason = _is_source_supported(raw, strict=strict)
        if not ok:
            dropped_claims.append(_drop_claim(raw, reason))
            continue

        constraint = _profile_constraint(unit, raw, index)
        if constraint["confidence"] == "uncertain":
            uncertain_constraints.append(constraint)
        else:
            constraints.append(constraint)

    return {
        "unit_id": unit["unit_id"],
        "engine": unit["engine"],
        "refactoring": unit["refactoring"],
        "phase": unit["phase"],
        "entry_name": unit["entry_name"],
        "relative_path": unit["relative_path"],
        "analysis_status": "ok",
        "constraints": constraints,
        "uncertain_constraints": uncertain_constraints,
        "dropped_claims": dropped_claims,
    }


def build_group_profile(
    engine: str,
    refactoring: str,
    repo_root: str,
    corpus_root: str,
    unit_profiles: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    strict: bool = True,
) -> Dict[str, Any]:
    constraints: List[Dict[str, Any]] = []
    uncertain_constraints: List[Dict[str, Any]] = []
    dropped_claims: List[Dict[str, Any]] = []
    source_units: List[Dict[str, Any]] = []

    for profile in unit_profiles:
        source_units.append(
            {
                "unit_id": profile.get("unit_id", ""),
                "entry_name": profile.get("entry_name", ""),
                "relative_path": profile.get("relative_path", ""),
                "phase": profile.get("phase", ""),
                "analysis_status": profile.get("analysis_status", "ok"),
                "constraints": len(profile.get("constraints", [])),
                "uncertain_constraints": len(profile.get("uncertain_constraints", [])),
                "dropped_claims": len(profile.get("dropped_claims", [])),
            }
        )
        constraints.extend(profile.get("constraints", []))
        uncertain_constraints.extend(profile.get("uncertain_constraints", []))
        dropped_claims.extend(profile.get("dropped_claims", []))

    return {
        "schema_version": "precondition_profile_v1",
        "strict_mode": strict,
        "engine": engine,
        "refactoring": refactoring,
        "repo_root": repo_root,
        "corpus_root": corpus_root,
        "source_units": source_units,
        "constraints": constraints,
        "uncertain_constraints": uncertain_constraints,
        "dropped_claims": dropped_claims,
        "errors": errors,
        "summary": {
            "units": len(source_units),
            "constraints": len(constraints),
            "uncertain_constraints": len(uncertain_constraints),
            "dropped_claims": len(dropped_claims),
            "errors": len(errors),
        },
    }


def _is_source_supported(item: Dict[str, Any], strict: bool) -> Tuple[bool, str]:
    condition = str(item.get("condition", "")).strip()
    normalized = str(item.get("normalized_required_condition", "")).strip()
    evidence = item.get("evidence", [])
    if not condition:
        return False, "missing_condition"
    if not normalized:
        return False, "missing_normalized_required_condition"
    if not isinstance(evidence, list) or not any(str(value).startswith("main:") for value in evidence):
        return False, "missing_main_evidence"
    if strict and str(item.get("support_level", "")).strip().lower() == "low":
        return False, "low_support_level"
    return True, ""


def _drop_claim(item: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "condition": str(item.get("condition", "")).strip(),
        "normalized_required_condition": str(item.get("normalized_required_condition", "")).strip(),
        "semantic_condition": str(item.get("semantic_condition", "")).strip(),
        "reason": reason,
        "evidence": item.get("evidence", []) if isinstance(item.get("evidence", []), list) else [],
    }


def _profile_constraint(unit: Dict[str, Any], item: Dict[str, Any], index: int) -> Dict[str, Any]:
    source_condition = str(item.get("condition", "")).strip()
    required_condition = str(item.get("normalized_required_condition", "")).strip()
    formal = formalize_condition(source_condition, required_condition)
    evidence = item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []
    support_level = str(item.get("support_level", "")).strip() or "medium"

    return {
        "constraint_id": f"{unit['unit_id']}.C{index:03d}",
        "from_units": [unit["unit_id"]],
        "phase": unit["phase"],
        "kind": "precondition",
        "category": str(item.get("category", "direct_precondition")).strip() or "direct_precondition",
        "polarity": str(item.get("polarity", "required")).strip() or "required",
        "severity": str(item.get("level", "WARNING")).strip() or "WARNING",
        "effect": _effect_for(item),
        "source_condition": source_condition,
        "normalized_required_condition": required_condition,
        "canonical": formal["canonical"],
        "formal": {
            key: value
            for key, value in formal.items()
            if key != "canonical"
        },
        "semantic": str(item.get("semantic_condition", "")).strip(),
        "message": str(item.get("error_message", "")).strip(),
        "evidence": [
            {
                "ref": str(value),
                "kind": "source" if str(value).startswith("main:") else "context",
            }
            for value in evidence
            if str(value).strip()
        ],
        "support": {
            "level": support_level,
            "depends_on": item.get("depends_on", []) if isinstance(item.get("depends_on", []), list) else [],
            "comparison_key": str(item.get("comparison_key", "")).strip(),
        },
        "confidence": "source_supported" if support_level in {"medium", "high"} else "uncertain",
    }


def formalize_condition(source_condition: str, required_condition: str) -> Dict[str, str]:
    """Best-effort formal condition for downstream consistency comparison."""
    source = re.sub(r"\s+", " ", source_condition.strip())
    required = re.sub(r"\s+", " ", required_condition.strip())

    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_.$()]*)\s+instanceof\s+([A-Za-z_][A-Za-z0-9_.$]*)\b", source)
    if match:
        subject, obj = match.group(1), match.group(2)
        return {
            "predicate": "is_instance_of",
            "subject": subject,
            "object": obj,
            "canonical": f"is_instance_of({subject}, {obj})",
        }

    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_.$()]*)\s*!=\s*null\b", source)
    if match:
        subject = match.group(1)
        return {
            "predicate": "is_non_null",
            "subject": subject,
            "object": "null",
            "canonical": f"is_non_null({subject})",
        }

    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_.$()]*)\s*==\s*null\b", source)
    if match:
        subject = match.group(1)
        return {
            "predicate": "is_non_null",
            "subject": subject,
            "object": "null",
            "canonical": f"is_non_null({subject})",
        }

    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_.$()]+)\s*==\s*([A-Za-z_][A-Za-z0-9_.$()]+)\b", source)
    if match:
        subject, obj = match.group(1), match.group(2)
        return {
            "predicate": "equals",
            "subject": subject,
            "object": obj,
            "canonical": f"equals({subject}, {obj})",
        }

    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_.$()]*)\.hasModifierProperty\(([^)]+)\)", source)
    if match:
        subject, obj = match.group(1), match.group(2).strip()
        return {
            "predicate": "has_modifier",
            "subject": subject,
            "object": obj,
            "canonical": f"has_modifier({subject}, {obj})",
        }

    canonical = normalize_text_key(required or source)
    return {
        "predicate": "satisfies",
        "subject": "",
        "object": required or source,
        "canonical": canonical,
    }


def _effect_for(item: Dict[str, Any]) -> str:
    polarity = str(item.get("polarity", "")).lower()
    category = str(item.get("category", "")).lower()
    if polarity == "blocking":
        return "blocks_refactoring"
    if polarity == "conflict" or category == "delegated_conflict_rule":
        return "reports_conflict_or_diverts"
    return "required_to_proceed"
