import json
import time

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from langchain_openai import ChatOpenAI

from precondition_agent.schemas import AnalysisConfig, TraceEvent
from precondition_agent.utils import extract_json, message_to_text, normalize_text_key


PROFILE_SCHEMA = "precondition_profile_v1"
COMPARISON_SCHEMA = "precondition_comparison_v1"


class ConsistencyComparisonAgent:
    """Compare strict precondition profiles across refactoring engines."""

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

    def serialize_trace(self) -> List[Dict[str, Any]]:
        if self.config.trace_level == "none":
            return []
        if self.config.trace_level == "compact":
            return [
                {
                    "stage": event.stage,
                    "payload": {
                        key: value
                        for key, value in event.payload.items()
                        if key
                        in {
                            "status",
                            "profiles_count",
                            "engines",
                            "refactoring",
                            "common_conditions_count",
                            "conflicts_count",
                            "test_scenarios_count",
                            "chars",
                            "error",
                        }
                    },
                    "duration_ms": event.duration_ms,
                }
                for event in self.trace
            ]
        return [asdict(event) for event in self.trace]

    def _llm_json(self, prompt: str, default: Dict[str, Any]) -> Dict[str, Any]:
        started_at = time.time()
        response = self.llm.invoke(prompt)
        text = message_to_text(response)
        try:
            parsed = extract_json(text)
            self._trace("llm_json", {"status": "ok", "chars": len(text)}, started_at)
            return parsed if isinstance(parsed, dict) else default
        except Exception as exc:
            self._trace(
                "llm_json",
                {"status": "fallback", "chars": len(text), "error": str(exc)},
                started_at,
            )
            return default

    def compare_profiles(
        self,
        profiles: List[Dict[str, Any]],
        refactoring: str = "",
        max_constraints_per_engine: int = 160,
    ) -> Dict[str, Any]:
        started_at = time.time()
        summaries = [
            summarize_profile(profile, max_constraints=max_constraints_per_engine)
            for profile in profiles
        ]
        inferred_refactoring = refactoring or infer_refactoring_name(summaries)
        prompt = build_comparison_prompt(summaries, inferred_refactoring)
        fallback = build_empty_report(summaries, inferred_refactoring)
        result = self._llm_json(prompt, fallback)
        report = normalize_comparison_report(result, summaries, inferred_refactoring)
        self._trace(
            "comparison_complete",
            {
                "status": "ok",
                "profiles_count": len(summaries),
                "engines": [item.get("engine", "") for item in summaries],
                "refactoring": inferred_refactoring,
                "common_conditions_count": len(report.get("common_conditions", [])),
                "conflicts_count": len(report.get("inconsistencies", [])),
                "test_scenarios_count": len(report.get("conflict_scenarios", [])),
            },
            started_at,
        )
        report["trace"] = self.serialize_trace()
        return report


def load_profile(path: str | Path) -> Dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"profile is not a JSON object: {path}")
    if payload.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError(f"unsupported profile schema in {path}: {payload.get('schema_version')}")
    payload["_profile_path"] = str(Path(path))
    return payload


def discover_profile_paths(
    corpus_root: str | Path,
    refactoring: str,
    engines: Optional[Iterable[str]] = None,
) -> List[Path]:
    root = Path(corpus_root)
    if (root / "corpus").exists():
        root = root / "corpus"
    wanted_refactoring = normalize_refactoring_key(refactoring)
    wanted_engines = {engine.strip().lower() for engine in engines or [] if engine.strip()}
    paths: List[Path] = []
    for path in sorted(root.rglob("precondition_profile.json")):
        try:
            profile = load_profile(path)
        except Exception:
            continue
        profile_refactoring = normalize_refactoring_key(str(profile.get("refactoring", "")))
        profile_engine = str(profile.get("engine", "")).strip()
        if wanted_refactoring and profile_refactoring != wanted_refactoring:
            continue
        if wanted_engines and profile_engine.lower() not in wanted_engines:
            continue
        paths.append(path)
    return paths


def summarize_profile(profile: Dict[str, Any], max_constraints: int = 160) -> Dict[str, Any]:
    constraints = profile.get("constraints", [])
    if not isinstance(constraints, list):
        constraints = []

    compact_constraints = [
        compact_constraint(item)
        for item in constraints[:max_constraints]
        if isinstance(item, dict)
    ]
    return {
        "profile_path": profile.get("_profile_path", ""),
        "engine": str(profile.get("engine", "")).strip(),
        "refactoring": str(profile.get("refactoring", "")).strip(),
        "strict_mode": bool(profile.get("strict_mode", True)),
        "summary": profile.get("summary", {}),
        "source_units": profile.get("source_units", []),
        "constraints_total": len(constraints),
        "constraints_truncated": len(constraints) > max_constraints,
        "constraints": compact_constraints,
        "uncertain_constraints_count": len(profile.get("uncertain_constraints", []) or []),
        "dropped_claims_count": len(profile.get("dropped_claims", []) or []),
        "errors_count": len(profile.get("errors", []) or []),
    }


def compact_constraint(item: Dict[str, Any]) -> Dict[str, Any]:
    evidence = item.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []
    support = item.get("support", {})
    if not isinstance(support, dict):
        support = {}
    formal = item.get("formal", {})
    if not isinstance(formal, dict):
        formal = {}
    return {
        "constraint_id": str(item.get("constraint_id", "")),
        "phase": str(item.get("phase", "")),
        "category": str(item.get("category", "")),
        "polarity": str(item.get("polarity", "")),
        "severity": str(item.get("severity", "")),
        "effect": str(item.get("effect", "")),
        "canonical": str(item.get("canonical", "")),
        "formal": {
            "predicate": str(formal.get("predicate", "")),
            "subject": str(formal.get("subject", "")),
            "object": str(formal.get("object", "")),
        },
        "normalized_required_condition": str(item.get("normalized_required_condition", "")),
        "semantic": str(item.get("semantic", "")),
        "message": str(item.get("message", "")),
        "comparison_key": str(support.get("comparison_key", "")),
        "support_level": str(support.get("level", "")),
        "depends_on": support.get("depends_on", []) if isinstance(support.get("depends_on", []), list) else [],
        "evidence_refs": [
            str(value.get("ref", ""))
            for value in evidence
            if isinstance(value, dict) and str(value.get("ref", "")).strip()
        ],
        "confidence": str(item.get("confidence", "")),
    }


def build_comparison_prompt(profile_summaries: List[Dict[str, Any]], refactoring: str) -> str:
    return f"""
You are a rigorous consistency-comparison agent for refactoring engines.

Goal:
Compare strict, source-supported precondition profiles for the same refactoring operation.
Identify semantically equivalent conditions, engine-specific conditions, true inconsistencies,
and conflict scenarios that can later drive differential refactoring-engine tests.

Rules:
1. Use only the provided profile data. Do not invent engine behavior or source-code facts.
2. Treat `canonical`, `formal`, `normalized_required_condition`, and `semantic` as evidence.
3. A difference is a true inconsistency only if it can plausibly change whether a refactoring is accepted,
   rejected, warned, or produces a different result.
4. Do not over-report wording differences as inconsistencies.
5. Preserve constraint IDs in `source_constraints` so every claim is traceable.
6. If evidence is insufficient, put the item in `unresolved_questions`, not in `inconsistencies`.
7. `conflict_scenarios` must be test-objective specifications, not Java code.
8. Do not collapse independent test objectives into one scenario. If two conditions need different Java
   program shapes, keep them as separate scenarios even when they belong to the same broad topic.
9. Also preserve lower-confidence but testable boundary differences in `conflict_scenarios` with
   `"confidence": "medium"` or `"confidence": "low"`.
10. Treat `engine_specific_conditions` and `unresolved_questions` as potential sources of candidate test
   scenarios when they are traceable to constraints and can be validated by later automated execution.
11. Prefer recall over early filtering for test generation: if a difference may plausibly affect accept,
   reject, warning, compilation, or behavior, keep it as a scenario even when the confidence is low.

Return JSON only with this schema:
{{
  "schema_version": "precondition_comparison_v1",
  "refactoring": "{refactoring}",
  "engines": ["..."],
  "common_conditions": [
    {{
      "topic": "short condition topic",
      "normalized_condition": "engine-neutral condition",
      "engines": ["..."],
      "source_constraints": ["constraint_id", "..."],
      "confidence": "high|medium|low"
    }}
  ],
  "equivalent_groups": [
    {{
      "topic": "short topic",
      "engine_constraints": [
        {{"engine": "...", "constraint_id": "...", "condition": "..."}}
      ],
      "equivalence_reason": "why these conditions are equivalent"
    }}
  ],
  "engine_specific_conditions": [
    {{
      "engine": "...",
      "topic": "short topic",
      "condition": "condition only found in this engine",
      "source_constraints": ["constraint_id", "..."],
      "possible_impact": "why this may matter"
    }}
  ],
  "inconsistencies": [
    {{
      "inconsistency_id": "C001",
      "topic": "short topic",
      "type": "missing_condition|stricter_vs_weaker|severity_mismatch|warning_vs_fatal|different_scope|other",
      "engines": ["..."],
      "difference": "precise difference",
      "risk": "why behavior may diverge",
      "source_constraints": ["constraint_id", "..."],
      "confidence": "high|medium|low"
    }}
  ],
  "conflict_scenarios": [
    {{
      "scenario_id": "S001",
      "derived_from": "C001",
      "test_objective": "what a later test generator should create",
      "program_shape": "minimal source-code shape needed",
      "operation_target": "where to apply the refactoring",
      "expected_behavior_difference": {{
        "EngineA": "reject|warn|allow|unknown plus short reason"
      }},
      "source_constraints": ["constraint_id", "..."],
      "confidence": "high|medium|low"
    }}
  ],
  "unresolved_questions": [
    {{
      "topic": "short topic",
      "reason": "what evidence is missing",
      "related_constraints": ["constraint_id", "..."]
    }}
  ],
  "notes": "short summary"
}}

Profiles:
{json.dumps(profile_summaries, ensure_ascii=False, indent=2)}
""".strip()


def build_empty_report(profile_summaries: List[Dict[str, Any]], refactoring: str) -> Dict[str, Any]:
    return {
        "schema_version": COMPARISON_SCHEMA,
        "refactoring": refactoring,
        "engines": [item.get("engine", "") for item in profile_summaries],
        "common_conditions": [],
        "equivalent_groups": [],
        "engine_specific_conditions": [],
        "inconsistencies": [],
        "conflict_scenarios": [],
        "unresolved_questions": [],
        "notes": "fallback_empty_report",
    }


def normalize_comparison_report(
    result: Dict[str, Any],
    profile_summaries: List[Dict[str, Any]],
    refactoring: str,
) -> Dict[str, Any]:
    report = build_empty_report(profile_summaries, refactoring)
    for key in [
        "common_conditions",
        "equivalent_groups",
        "engine_specific_conditions",
        "inconsistencies",
        "conflict_scenarios",
        "unresolved_questions",
    ]:
        value = result.get(key, [])
        report[key] = value if isinstance(value, list) else []
    report["schema_version"] = COMPARISON_SCHEMA
    report["refactoring"] = str(result.get("refactoring") or refactoring)
    engines = result.get("engines")
    report["engines"] = engines if isinstance(engines, list) and engines else [
        item.get("engine", "") for item in profile_summaries
    ]
    report["notes"] = str(result.get("notes", "")).strip()
    report["inputs"] = [
        {
            "engine": item.get("engine", ""),
            "refactoring": item.get("refactoring", ""),
            "profile_path": item.get("profile_path", ""),
            "constraints_total": item.get("constraints_total", 0),
            "constraints_used": len(item.get("constraints", [])),
            "constraints_truncated": item.get("constraints_truncated", False),
        }
        for item in profile_summaries
    ]
    return report


def infer_refactoring_name(profile_summaries: List[Dict[str, Any]]) -> str:
    names = [str(item.get("refactoring", "")).strip() for item in profile_summaries if item.get("refactoring")]
    if not names:
        return ""
    counts: Dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


def normalize_refactoring_key(value: str) -> str:
    text = value.replace("\\", "/").split("/")[-1] if "/" in value.replace("\\", "/") else value
    return normalize_text_key(text)
