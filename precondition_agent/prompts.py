import json

from typing import Any, Dict, List


def build_symbol_probe_prompt(
    snippet: str,
    effective_entry_name: str,
    decisive_conditions: List[str],
    candidate_symbols: List[Dict[str, Any]] | None = None,
) -> str:
    candidate_symbols = candidate_symbols or []
    return f"""
You are an analysis agent for refactoring preconditions.

Task:
Given one Java or Kotlin method-sized snippet, review the program-generated precondition boundary
and choose only from the provided candidate symbols. The candidate list is a closed set produced by
static slicing and filtering; do not add symbols that are not present in that list.

The input may contain setup code, logging, UI/execution wrappers, ordinary plumbing, or result
construction. Do not treat the whole method body as precondition logic by default.

Interpret "unknown" pragmatically:
- keep a symbol if the snippet alone does not reveal what the symbol really is, what it returns, or
  what internal checks it performs
- drop a symbol if the snippet already makes its role obvious enough for precondition analysis
- drop ordinary containers, common framework accessors, parameter names, local variables, and UI/execution wrappers

Use only exact `symbol` values from Candidate symbols. Do not invent repository symbols. If an
important symbol seems missing, mention it in `skip` with a reason instead of adding it to
unknown_symbols.

Typical keep cases:
- helper methods whose implementation contains the real decision logic
- helper methods that produce a value later checked by a guard
- delegated conflict/check/validate helpers that run after direct guards; they still define the
  effective refactoring precondition or conflict rule boundary
- exception types that change control flow
- special framework/internal types whose semantics are not obvious from the snippet
- state-bearing fields/properties only if their definition is needed to understand the guard

Typical drop cases:
- container types like maps/lists
- obvious getters like getContainingClass when they only provide plumbing
- ordinary parameters, fields, or local variables mentioned only as helper arguments
- possible NullPointerException assumptions unless the snippet contains an explicit null/type guard
- duplicate bare method names when a qualified owner.method version is already present
- symbols whose semantics are already explicit in the snippet

Return JSON only:
{{
  "decisive_conditions": ["..."],
  "precondition_slices": [
    {{
      "range": "L3-L5",
      "code": "exact code from the snippet",
      "kind": "direct_guard|dependency_setup|delegated_checker|exception_gate|state_signal|supporting_context",
      "condition": "source condition when applicable",
      "reason": "why this code belongs to the precondition boundary"
    }}
  ],
  "unknown_symbols": [
    {{
      "symbol": "exact symbol name",
      "kind": "method|type|field|property|exception",
      "role": "direct_guard|delegated_checker|guard_value_producer|exception_gate|state_signal|supporting_type",
      "priority": 1,
      "why_unknown": "why the snippet is insufficient to understand this symbol",
      "why_it_matters": "why this missing meaning affects the final precondition analysis",
      "preferred_context": "method_definition|type_definition|property_definition|call_container"
    }}
  ],
  "skip": [
    {{
      "token": "candidate symbol not selected",
      "reason": "why it is understandable enough or not important enough"
    }}
  ]
}}

Entry name hint:
{effective_entry_name or "unknown"}

Current decisive conditions:
{json.dumps(decisive_conditions, ensure_ascii=False, indent=2)}

Candidate symbols:
{json.dumps(candidate_symbols, ensure_ascii=False, indent=2)}

Precondition snippet:
```text
{snippet}
```
""".strip()


def build_symbol_grounding_prompt(
    current_snippet: str,
    payload: List[Dict[str, Any]],
) -> str:
    return f"""
You are grounding repository-specific symbols for refactoring precondition analysis.

Task:
For each symbol, infer a short semantic meaning from the selected repository contexts.
The meaning should be easier to understand than the raw symbol name itself.

Rules:
1. Do not simply repeat the raw symbol name as the grounded meaning.
2. Prefer phrases like "a PSI method element", "a lightweight synthetic method builder object",
   "a Kotlin function PSI node", "a Kotlin extension helper", "a Kotlin property",
   or "a helper that checks method conflicts".
3. Keep the wording short, concrete, and suitable for later use in semantic_condition fields.
4. If the contexts are insufficient, still provide the best grounded meaning you can, but lower confidence.
5. Prefer meaning phrases that could later be compared across engines, e.g. "method declaration element",
   "synthetic generated method representation", "helper that validates extraction options".

Return JSON only:
{{
  "grounded_symbols": [
    {{
      "symbol": "PsiMethod",
      "kind": "type",
      "grounded_meaning": "a PSI representation of a method",
      "role_in_snippet": "used as the positive type gate",
      "evidence": ["PsiMethod.java:23-27"],
      "confidence": "high|medium|low"
    }}
  ]
}}

Current snippet:
```text
{current_snippet}
```

Symbols and selected contexts:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def build_ast_context_planning_prompt(
    current_snippet: str,
    item: Dict[str, Any],
    limit: int,
    candidates: List[Dict[str, Any]],
) -> str:
    return f"""
You are planning which AST context blocks to read for refactoring precondition analysis.

Goal:
Choose the smallest set of AST blocks that are sufficient to explain the target symbol.

Current snippet:
```text
{current_snippet}
```

Target symbol:
- symbol: {item.get("symbol", "")}
- kind: {item.get("kind", "")}
- reason: {item.get("reason", "")}

Selection rules:
1. Select at most {limit} blocks.
2. Prefer blocks that explain the symbol's real semantics, not generic boilerplate.
3. For type symbols, select the declaration header when it explains identity, inheritance, or interfaces.
4. Select constructors only when construction semantics matter.
5. Select related methods only when they explain why the symbol matters for the current precondition.
6. For Kotlin, prefer extension/top-level functions, property declarations/getters, safe-cast guards,
   Elvis-return guards, safe-call chains, and `when` branches when those blocks explain the decision.
7. Prefer focused semantic sub-blocks such as guard_condition_block, delegation_block,
   exception_handling_block, or return_effect_block when they are sufficient; avoid selecting a full method
   if a smaller block already captures the decisive semantics.

Return JSON only:
{{
  "selected_ids": [0, 2],
  "reason": "short explanation"
}}

Candidate AST blocks:
{json.dumps(candidates, ensure_ascii=False, indent=2)}
""".strip()


def build_retrieval_rerank_prompt(
    item: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> str:
    return f"""
You are a retrieval judge for refactoring precondition analysis.

Target symbol:
- symbol: {item.get("symbol", "")}
- kind: {item.get("kind", "")}
- intended_region: {item.get("region", "any")}
- reason: {item.get("reason", "")}

Choose the most useful search hits that are likely to provide:
- the actual definition
- the implementation body
- or the decisive semantic context

If the candidates are AST context blocks, prefer the smallest combination that best explains:
- what the symbol is
- why it matters for the current precondition
- and how it affects the boolean decision logic

Avoid random call sites unless they are the only useful evidence.

Return JSON only:
{{
  "selected_ids": [0, 2],
  "reason": "..."
}}

Candidate hits:
{json.dumps(candidates, ensure_ascii=False, indent=2)}
""".strip()


def build_synthesis_prompt(
    engine: str,
    entry_name: str,
    snippet: str,
    probe_result: Dict[str, Any],
    grounded_payload: List[Dict[str, Any]],
    context_payload: List[Dict[str, Any]],
) -> str:
    return f"""
You are a formal analysis engine for refactoring preconditions.

Task:
Given one Java or Kotlin snippet and the repository contexts selected for its semantic boundaries,
produce a normalized precondition analysis.

Rules:
1. Extract only conditions that materially affect whether the refactoring proceeds, stops, or records a conflict.
2. Distinguish between:
   - direct preconditions of the current snippet
   - delegated conflict rules that come from helper methods called by the snippet
3. If a symbol is still unclear, keep it in unknown_symbols instead of guessing.
4. Prefer comparison-friendly positive formulations when possible.
5. Keep `condition` source-faithful, but use grounded symbol meanings in `semantic_condition`
   and `normalized_required_condition` whenever possible.
6. Avoid repeating raw repository-specific names such as PsiMethod or LightMethodBuilder in
   `semantic_condition` if a clearer grounded meaning is available below.
7. For Kotlin, expand control-flow expressions into explicit conditions:
   - `x as? T ?: return false` means x must be safely castable to T.
   - `x ?: return false` means x must be non-null.
   - `x?.foo() == true` means x must be non-null and foo() must be true.
   - `when (x) {{ is T -> ... else -> false }}` means x must match an accepted branch type.
   - `!is T` is a blocking type guard unless the surrounding branch reverses it.
8. Be conservative: do not infer that a returned list must be non-empty unless the code explicitly checks emptiness.
9. Treat readAction/withBackgroundProgress/withContext as execution wrappers, not refactoring preconditions.
10. Treat project/editor validity and UI error hint display as supporting environment/UI facts, not core preconditions,
    unless the snippet explicitly checks them.
11. If the snippet mainly delegates analysis to a helper function, prefer a compact delegated rule for that helper.
    Do not expand deep internal details such as PsiClass/PsiFile hierarchy unless the selected context contains
    explicit checks and enough surrounding code to justify them.
12. Prefer concise, comparison-friendly conditions that could later be matched across different refactoring engines.

Return JSON only with this schema:
{{
  "engine": "{engine}",
  "entry_name": "{entry_name}",
  "rule_name": "best-effort refactoring rule name",
  "constraints": [
    {{
      "condition": "normalized formal English condition",
      "semantic_condition": "same condition rewritten in semantic/natural-language form",
      "error_message": "what failure/conflict/stop this condition implies",
      "level": "FATAL|WARNING",
      "category": "direct_precondition|delegated_conflict_rule|supporting_fact",
      "polarity": "required|blocking|conflict",
      "normalized_required_condition": "positive/comparison-friendly form when applicable",
      "evidence": ["main:L10", "ctx_2:L45-L82"],
      "depends_on": ["symbolA", "symbolB"]
    }}
  ],
  "direct_preconditions": ["copy of the direct_precondition constraints"],
  "delegated_conflict_rules": ["copy of the delegated_conflict_rule constraints"],
  "decisive_conditions": ["verbatim or normalized decisive checks"],
  "unknown_symbols": ["..."],
  "notes": "short explanation"
}}

Main snippet:
```text
{snippet}
```

Boundary detection result:
{json.dumps(probe_result, ensure_ascii=False, indent=2)}

Grounded symbols:
{json.dumps(grounded_payload, ensure_ascii=False, indent=2)}

Selected contexts:
{json.dumps(context_payload, ensure_ascii=False, indent=2)}
""".strip()


def build_compare_engines_prompt(analyses: List[Dict[str, Any]]) -> str:
    return f"""
You are comparing precondition analyses from multiple refactoring engines.

Task:
1. Identify common constraints shared by most engines.
2. Identify engine-specific constraints.
3. Identify true inconsistencies that may lead to divergent refactoring behavior.
4. Suggest test-case focus points for differential testing.

Return JSON only:
{{
  "common_constraints": ["..."],
  "engine_specific": [
    {{
      "engine": "IntelliJ",
      "constraints": ["..."]
    }}
  ],
  "inconsistencies": [
    {{
      "topic": "...",
      "engines": ["IntelliJ", "Eclipse"],
      "difference": "...",
      "risk": "..."
    }}
  ],
  "suggested_test_focus": ["..."]
}}

Analyses:
{json.dumps(analyses, ensure_ascii=False, indent=2)}
""".strip()
