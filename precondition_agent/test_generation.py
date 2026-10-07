import json
import re
import time

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from langchain_openai import ChatOpenAI

from precondition_agent.schemas import AnalysisConfig, TraceEvent
from precondition_agent.utils import extract_json, message_to_text


TEST_CASE_SCHEMA = "refactoring_test_cases_v1"
DEFAULT_TEST_PACKAGE = "generated.refactoring.tests"
DEFAULT_EXTRACTED_VARIABLE_NAME = "extractedValue"
DEFAULT_EXTRACTED_METHOD_NAME = "extractedMethod"
JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while", "true", "false",
    "null", "var", "yield", "record", "sealed", "permits", "non-sealed",
}


class TestGenerationAgent:
    """Generate targeted Java test cases from precondition comparison reports."""

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
                            "refactoring",
                            "scenarios_count",
                            "test_cases_count",
                            "tests_per_scenario",
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

    def generate_tests(
        self,
        comparison_report: Dict[str, Any],
        max_scenarios: int = 3,
        tests_per_scenario: int = 10,
        source_report: str = "",
        layout: str = "simple",
        scenario_ids: Optional[Iterable[str]] = None,
        extra_guidance: str = "",
    ) -> Dict[str, Any]:
        started_at = time.time()
        scenarios = select_generation_scenarios(
            comparison_report,
            max_scenarios=max_scenarios,
            scenario_ids=scenario_ids,
        )
        prompt = build_test_generation_prompt(
            comparison_report,
            scenarios,
            tests_per_scenario,
            extra_guidance=extra_guidance,
        )
        fallback = build_empty_test_suite(comparison_report, source_report, tests_per_scenario)
        raw = self._llm_json(prompt, fallback)
        suite = normalize_test_suite(
            raw,
            comparison_report,
            source_report=source_report,
            tests_per_scenario=tests_per_scenario,
            layout=layout,
        )
        if extra_guidance.strip():
            suite.setdefault("generation_policy", {})["extra_guidance"] = extra_guidance.strip()
        suite["trace"] = self.serialize_trace()
        self._trace(
            "test_generation_complete",
            {
                "status": "ok",
                "refactoring": suite.get("refactoring", ""),
                "scenarios_count": len(scenarios),
                "test_cases_count": len(suite.get("test_cases", [])),
                "tests_per_scenario": tests_per_scenario,
            },
            started_at,
        )
        suite["trace"] = self.serialize_trace()
        return suite


def load_comparison_report(path: str | Path) -> Dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"comparison report is not a JSON object: {path}")
    if payload.get("schema_version") != "precondition_comparison_v1":
        raise ValueError(f"unsupported comparison report schema: {payload.get('schema_version')}")
    payload["_comparison_path"] = str(Path(path))
    return payload


def select_generation_scenarios(
    comparison_report: Dict[str, Any],
    max_scenarios: int,
    scenario_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    inconsistencies = {
        str(item.get("inconsistency_id", "")): item
        for item in comparison_report.get("inconsistencies", []) or []
        if isinstance(item, dict)
    }
    wanted = {str(value).strip() for value in scenario_ids or [] if str(value).strip()}
    selected: List[Dict[str, Any]] = []
    for raw in comparison_report.get("conflict_scenarios", []) or []:
        if not isinstance(raw, dict):
            continue
        scenario = dict(raw)
        scenario_id = str(scenario.get("scenario_id", ""))
        if wanted and scenario_id not in wanted:
            continue
        derived_from = str(scenario.get("derived_from", ""))
        if derived_from and derived_from in inconsistencies:
            scenario["inconsistency"] = inconsistencies[derived_from]
        selected.append(scenario)
        if max_scenarios and len(selected) >= max_scenarios:
            return selected

    # Some comparison reports may have inconsistencies but no explicit scenarios.
    if selected:
        return selected

    for inc in inconsistencies.values():
        scenario_id = f"S{len(selected) + 1:03d}"
        selected.append(
            {
                "scenario_id": scenario_id,
                "derived_from": inc.get("inconsistency_id", ""),
                "test_objective": inc.get("risk", "") or inc.get("difference", ""),
                "program_shape": "Construct a minimal Java program that exercises this inconsistency.",
                "operation_target": "Apply the target refactoring at the marked location.",
                "expected_behavior_difference": {},
                "source_constraints": inc.get("source_constraints", []),
                "inconsistency": inc,
            }
        )
        if max_scenarios and len(selected) >= max_scenarios:
            break
    return selected


def build_test_generation_prompt(
    comparison_report: Dict[str, Any],
    scenarios: List[Dict[str, Any]],
    tests_per_scenario: int,
    extra_guidance: str = "",
) -> str:
    refactoring = str(comparison_report.get("refactoring", "")).strip()
    engines = comparison_report.get("engines", [])
    guidance = generation_guidance_for_refactoring(refactoring)
    return f"""
You are a test generation agent for refactoring-engine differential testing.

Task:
Generate targeted Java test programs from precondition inconsistencies and conflict scenarios.
The tests will later be run before and after refactoring to detect behavior changes, compile errors,
or divergent refactoring-engine decisions.

Global requirements:
1. Use English for all metadata, notes, objectives, and Java comments.
2. Generate exactly {tests_per_scenario} tests for each input scenario unless the scenario is impossible.
3. The {tests_per_scenario} tests for one scenario must be semantically diverse, not superficial renamings.
   Each test must cover a distinct semantic subcase whenever possible, not only a different parameter type
   or a different call location.
4. Each test case must be a standalone Java program with one public class and a deterministic `main` method.
5. Each Java program must print deterministic output for before/after behavior comparison.
6. Each Java program must include a clear comment starting with `// Refactoring operation:`.
7. Each Java program must mark the target location or method with comments when possible.
8. Each Java program must declare exactly this package: `package {DEFAULT_TEST_PACKAGE};`.
9. Do not use external libraries. Prefer plain Java source compatible with Java 17.
10. Keep programs small, but include inheritance, interfaces, overloads, visibility, fields, exceptions,
   or nested classes when those structures are needed to cover the scenario.
11. If a test intentionally targets a rejection boundary, the source should still compile before refactoring
    unless the scenario explicitly concerns uncompilable input.
12. Do not invent extra engine facts. Expected behavior may be `unknown` if the report does not justify it.
13. Return JSON only. Do not wrap it in Markdown.
14. Every test case must include `variant_category`, a short snake_case label for the semantic subcase.
15. For one scenario, avoid reusing the same `variant_category` unless there are more tests than meaningful subcases.
16. If the scenario is about missing method bodies, distribute tests across semantic categories such as
    native_method, abstract_method, interface_declaration, abstract_class_declaration, inherited_abstract_method,
    unavailable_source, overloaded_bodyless_method, polymorphic_bodyless_call, and method_reference_to_bodyless_method.

Refactoring-specific guidance:
{guidance}

Additional user guidance:
{extra_guidance.strip() or "None."}

Output schema:
{{
  "schema_version": "refactoring_test_cases_v1",
  "refactoring": "{refactoring}",
  "engines": {json.dumps(engines, ensure_ascii=False)},
  "generation_policy": {{
    "language": "English",
    "tests_per_scenario": {tests_per_scenario},
    "requires_main": true,
    "requires_refactoring_comments": true,
    "requires_behavior_output": true
  }},
  "test_cases": [
    {{
      "test_id": "{safe_refactoring_name(refactoring)}_S001_T01",
      "derived_from": "S001",
      "source_inconsistency": "C001",
      "variant": "minimal direct case",
      "variant_category": "semantic_subcase_label",
      "objective": "what this test verifies",
      "refactoring": "{refactoring}",
      "operation": {{
        "type": "{operation_type_for_refactoring(refactoring)}",
        "target_kind": "method, field, variable, expression, or empty",
        "target_method": "methodName or empty",
        "target_field": "old field name for RenameField, otherwise empty",
        "target_variable": "old local-variable name for RenameVariable, otherwise empty",
        "target_symbol": "symbol or empty",
        "old_name": "old identifier for Rename operations, otherwise empty",
        "target_expression": "exact source substring for ExtractVariable, otherwise empty",
        "selected_code": "exact source substring for ExtractMethod, otherwise empty",
        "target_occurrence": 1,
        "new_name": "new name for Rename*, ExtractVariable, or ExtractMethod; otherwise empty",
        "replace_all": false,
        "replace_duplicates": false,
        "target_location_hint": "where to apply the refactoring"
      }},
      "expected_behavior": {{
        "Eclipse": "reject|warn|allow|unknown plus short reason"
      }},
      "oracle": {{
        "compile_before": true,
        "run_before": true,
        "compile_after": true,
        "run_after": true,
        "compare_stdout": true,
        "behavior_should_preserve": true
      }},
      "files": [
        {{
          "path": "src/{safe_refactoring_name(refactoring)}_S001_T01.java",
          "main_class": "{DEFAULT_TEST_PACKAGE}.{safe_refactoring_name(refactoring)}_S001_T01",
          "content": "complete Java source code"
        }}
      ],
      "needs_human_review": false,
      "notes": "why this test follows from the scenario"
    }}
  ]
}}

Input scenarios:
{json.dumps(scenarios, ensure_ascii=False, indent=2)}

Comparison context:
{json.dumps(compact_report_context(comparison_report), ensure_ascii=False, indent=2)}
""".strip()


def generation_guidance_for_refactoring(refactoring: str) -> str:
    key = refactoring.lower().replace(" ", "").replace("/", "")
    if "inlinemethod" in key:
        return "\n".join(
            [
                "- For Inline Method, vary recursion, native/no-body method declarations, interface or abstract dispatch, overriding, access compatibility, void returns, constructor-like calls, side effects, and control flow.",
                "- For recursive-method scenarios, use semantic categories such as direct_recursion, mutual_recursion, recursion_via_interface_dispatch, recursion_with_overload, recursion_in_loop_context, recursion_with_side_effect, recursion_with_exception_path, inherited_recursive_method, static_recursive_method, and generic_recursive_method.",
                "- For native/no-body scenarios, do not generate only native-method variants. Use semantic categories such as native_method, abstract_method, interface_declaration, abstract_class_declaration, inherited_abstract_method, unavailable_source, overloaded_bodyless_method, polymorphic_bodyless_call, method_reference_to_bodyless_method, and lambda_call_to_bodyless_method.",
                "- For interface or abstract-dispatch scenarios, separate abstract interface declarations, default interface methods, single implementation, multiple implementations, abstract superclass methods, and polymorphic call sites.",
                "- For access-compatibility scenarios, separate private field access, package-private member access, protected access across packages, nested class access, and inherited member access.",
                "- Mark the invocation site that should be inlined.",
                "- Prefer a `run()` method called by `main()` so stdout can be compared.",
            ]
        )
    if "extractvariable" in key or "introducevariable" in key or "extractlocalvariable" in key:
        return "\n".join(
            [
                "- The operation object must use `type: \"ExtractVariable\"`, a non-empty `target_expression`, a positive `target_occurrence`, a valid non-conflicting `new_name`, and a boolean `replace_all`.",
                "- `target_expression` must be the exact Java source substring selected in the program. Do not emit character offsets; the runner computes offsets using each IDE's document model.",
                "- Place `// Refactoring operation: Extract Variable ...` immediately before the statement or declaration containing the selected expression.",
                "- If the same expression occurs multiple times after the marker, use `target_occurrence` to identify the intended occurrence. Set `replace_all: true` only when all equivalent occurrences should be replaced.",
                "- The selected expression must be syntactically complete. For `new int[] {1, 2}`, target the whole `new int[] {1, 2}` expression rather than only `{1, 2}`; a bare `{1, 2}` target is valid only when it is the complete declaration initializer.",
                "- Keep `new_name` a legal Java identifier that is not a keyword and does not already conflict in the extraction scope. Use `extractedValue` when no scenario-specific name is needed.",
                "- Generate compilable pre-refactoring programs and deterministic output. Rejection-boundary tests must still compile before refactoring.",
                "- Vary assignment expressions, null literals, array initializers, repeated expressions, side effects, nested expressions, lambdas, conditionals, field initializers, and static or instance initializer contexts only when justified by the scenario.",
            ]
        )
    if "extractmethod" in key or "introducemethod" in key:
        return "\n".join(
            [
                "- The operation object must use `type: \"ExtractMethod\"`, a non-empty `selected_code`, a positive `target_occurrence`, a valid non-conflicting `new_name`, and a boolean `replace_duplicates`.",
                "- `selected_code` must be the exact contiguous Java source substring a user would select in the editor. Do not emit character offsets; the runner computes IDE-specific offsets.",
                "- Select either one complete expression or one or more complete adjacent statements. Never select partial tokens, comments alone, or unmatched braces unless the scenario intentionally tests rejection.",
                "- Place `// Refactoring operation: Extract Method` immediately before the selected expression or first selected statement.",
                "- Use `target_occurrence` when the exact selected text appears more than once. Keep `replace_duplicates: false` unless duplicate replacement is itself the intended scenario.",
                "- Keep `new_name` a legal Java method identifier that does not conflict unless the scenario intentionally tests a name conflict.",
                "- Every pre-refactoring program must compile and run with deterministic output, including rejection-boundary cases.",
                "- Vary expression extraction, multiple statements, inputs, outputs, local variables, checked exceptions, loops, branches, lambdas, synchronized code, generics, and duplicate fragments only when justified by the conflict scenario.",
            ]
        )
    if "renamefield" in key:
        return "\n".join(
            [
                "- Generate only field-rename tests. Do not generate method, class, package, parameter, or local-variable rename tests.",
                "- The operation object must use `type: \"RenameField\"`, `target_kind: \"field\"`, and non-empty `target_field`, `target_symbol`, `old_name`, and `new_name` values.",
                "- `target_field`, `target_symbol`, and `old_name` must all be the old field identifier. `new_name` must be the intended replacement identifier.",
                "- Put the exact inline marker `/* rename target field */` between the declared field type and old identifier, for example `int /* rename target field */ oldName = 1;`. A separate comment is not sufficient for unambiguous runner targeting.",
                "- Keep the marked declaration in source code available to the IDE. Do not simulate binary-only or unavailable-source fields in an ordinary standalone Java file.",
                "- For scenarios requiring broken source, a binary/library declaration, or a closed project, set `needs_human_review: true` and explain the external setup in `notes`; do not fake that setup with a normal compilable source file.",
                "- Vary same-name renames, same-type collisions, enum constants, static imports, inner or enclosing class shadowing, accessor-related behavior, and naming-policy boundaries only when justified by the scenario.",
                "- Ensure `main()` reads or writes the affected field and prints deterministic output so call-site updates and behavior preservation can be checked.",
            ]
        )
    if "renamevariable" in key or "renamelocalvariable" in key:
        return "\n".join(
            [
                "- Generate only local-variable rename tests. Do not generate field, method, class, package, or parameter rename tests.",
                "- The operation object must use `type: \"RenameVariable\"`, `target_kind: \"variable\"`, and non-empty `target_variable`, `target_symbol`, `old_name`, and `new_name` values.",
                "- `target_variable`, `target_symbol`, and `old_name` must all be the old local-variable identifier. `new_name` must be the intended replacement identifier.",
                "- Put the exact inline marker `/* rename target variable */` between the declared variable type and old identifier, for example `int /* rename target variable */ oldName = 1;`. A separate comment is not sufficient for unambiguous runner targeting.",
                "- The marked declaration must be a real local variable with at least one observable use. Avoid parameters, fields, pattern variables, and labels unless a future operation type explicitly supports them.",
                "- Vary collisions with fields, same-scope local collisions, naming-policy warnings, and for-loop initializer variables only when justified by the scenario.",
                "- Every pre-refactoring program must compile and run with deterministic output, including tests expected to be rejected by a refactoring engine.",
            ]
        )
    if "renamemethod" in key:
        return "\n".join(
            [
                "- Generate only method-rename tests. Do not generate constructor, class, field, package, or local-variable rename tests.",
                "- The operation object must use `type: \"RenameMethod\"`, a non-empty `target_method`, a non-empty `target_symbol`, and a non-empty `new_name`.",
                "- `target_method` must be the old method name only, for example `oldName`; `target_symbol` should include parentheses, for example `oldName()`.",
                "- `new_name` must be the intended replacement method name. For conflict tests, choose a name that already conflicts with another method when the scenario requires it.",
                "- For Rename Method, vary overloads, overrides, interface implementations, name conflicts, visibility, call-site updates, inherited methods, and static versus instance methods.",
                "- Use semantic categories such as overload_conflict, override_conflict, interface_implementation, inherited_call_site, static_method, private_method, package_private_method, nested_class_call, generic_method, and method_reference.",
                "- Mark the declaration to rename and the intended new name with comments such as `// Refactoring operation: Rename Method oldName() to newName`.",
                "- Ensure `main()` calls the affected method before refactoring so stdout can be compared after rename.",
            ]
        )
    if "moveinstancemethod" in key:
        return "\n".join(
            [
                "- For Move Instance Method, vary target receiver selection, this-reference use, field access, visibility, parameters, inheritance, aliases, and side effects.",
                "- Use semantic categories such as this_field_access, target_field_access, parameter_dependency, private_member_access, inherited_member_access, alias_receiver, null_receiver_boundary, static_member_use, side_effect_order, and inner_class_dependency.",
                "- Mark the method to move and the intended target class or receiver.",
                "- Ensure `main()` exercises the moved method behavior before refactoring.",
            ]
        )
    return "\n".join(
        [
            "- Vary boundary cases, conflicts, visibility, inheritance, overloads, side effects, and control flow according to the scenario.",
            "- Mark the exact refactoring target and intended operation in comments.",
            "- Ensure `main()` prints deterministic behavior-observation output.",
        ]
    )


def compact_report_context(comparison_report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "refactoring": comparison_report.get("refactoring", ""),
        "engines": comparison_report.get("engines", []),
        "common_conditions": comparison_report.get("common_conditions", [])[:12],
        "engine_specific_conditions": comparison_report.get("engine_specific_conditions", [])[:20],
        "inconsistencies": comparison_report.get("inconsistencies", [])[:20],
        "unresolved_questions": comparison_report.get("unresolved_questions", [])[:12],
        "notes": comparison_report.get("notes", ""),
    }


def normalize_test_suite(
    raw: Dict[str, Any],
    comparison_report: Dict[str, Any],
    source_report: str = "",
    tests_per_scenario: int = 10,
    layout: str = "simple",
) -> Dict[str, Any]:
    refactoring = str(raw.get("refactoring") or comparison_report.get("refactoring") or "").strip()
    engines = raw.get("engines") if isinstance(raw.get("engines"), list) else comparison_report.get("engines", [])
    cases: List[Dict[str, Any]] = []
    for index, raw_case in enumerate(raw.get("test_cases", []) or [], start=1):
        if not isinstance(raw_case, dict):
            continue
        test_id = safe_test_id(str(raw_case.get("test_id", "")), refactoring, index)
        class_name = safe_java_identifier(test_id)
        files = normalize_case_files(raw_case.get("files", []), class_name, layout=layout)
        operation = normalize_operation(raw_case.get("operation", {}), refactoring)
        validation_errors = []
        for item in files:
            validation_errors.extend(validate_java_test_content(item.get("content", "")))
        validation_errors.extend(validate_refactoring_operation(operation, files))
        needs_review = bool(raw_case.get("needs_human_review", False)) or bool(validation_errors)
        cases.append(
            {
                "test_id": test_id,
                "derived_from": str(raw_case.get("derived_from", "")),
                "source_inconsistency": str(raw_case.get("source_inconsistency", "")),
                "variant": str(raw_case.get("variant", "")),
                "variant_category": safe_variant_category(str(raw_case.get("variant_category", ""))),
                "objective": str(raw_case.get("objective", "")),
                "refactoring": str(raw_case.get("refactoring") or refactoring),
                "operation": operation,
                "expected_behavior": raw_case.get("expected_behavior", {}) if isinstance(raw_case.get("expected_behavior", {}), dict) else {},
                "oracle": normalize_oracle(raw_case.get("oracle", {})),
                "files": files,
                "needs_human_review": needs_review,
                "validation_errors": sorted(set(validation_errors)),
                "notes": str(raw_case.get("notes", "")),
            }
        )

    return {
        "schema_version": TEST_CASE_SCHEMA,
        "refactoring": refactoring,
        "source_comparison_report": source_report or comparison_report.get("_comparison_path", ""),
        "engines": engines,
        "generation_policy": {
            "language": "English",
            "tests_per_scenario": tests_per_scenario,
            "requires_main": True,
            "requires_refactoring_comments": True,
            "requires_behavior_output": True,
            "output_layout": output_layout_description(layout),
            "project_layout": normalize_layout(layout),
        },
        "test_cases": cases,
    }


def normalize_case_files(raw_files: Any, class_name: str, layout: str = "simple") -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    normalized_layout = normalize_layout(layout)
    if not isinstance(raw_files, list):
        raw_files = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            continue
        content = str(raw_file.get("content", "")).strip()
        main_class = infer_java_main_class(content, str(raw_file.get("main_class") or class_name))
        content = ensure_test_package(content)
        path = java_source_path(main_class, normalized_layout)
        main_class_value = qualified_main_class(main_class)
        files.append(
            {
                "path": path,
                "main_class": main_class_value,
                "content": content + ("\n" if content and not content.endswith("\n") else ""),
            }
        )
    if not files:
        files.append(
            {
                "path": java_source_path(class_name, normalized_layout),
                "main_class": qualified_main_class(class_name),
                "content": "",
            }
        )
    return files


def normalize_operation(raw: Any, refactoring: str) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    operation_type = operation_type_for_refactoring(str(raw.get("type") or refactoring))
    new_name = str(raw.get("new_name") or raw.get("target_name") or "").strip()
    if operation_type == "ExtractVariable" and not new_name:
        new_name = DEFAULT_EXTRACTED_VARIABLE_NAME
    if operation_type == "ExtractMethod" and not new_name:
        new_name = DEFAULT_EXTRACTED_METHOD_NAME
    target_method = str(raw.get("target_method") or "").strip()
    target_field = str(raw.get("target_field") or "").strip()
    target_variable = str(
        raw.get("target_variable") or raw.get("target_local_variable") or ""
    ).strip()
    target_symbol = str(raw.get("target_symbol") or "").strip()
    old_name = str(raw.get("old_name") or raw.get("source_name") or "").strip()
    target_kind = str(raw.get("target_kind") or "").strip().lower()
    if operation_type == "RenameMethod":
        target_kind = "method"
        old_name = old_name or target_method or target_symbol.removesuffix("()")
        target_method = target_method or old_name
        target_symbol = target_symbol or (f"{old_name}()" if old_name else "")
    elif operation_type == "RenameField":
        target_kind = "field"
        old_name = old_name or target_field or target_symbol
        target_field = target_field or old_name
        target_symbol = target_field
    elif operation_type == "RenameVariable":
        target_kind = "variable"
        old_name = old_name or target_variable or target_symbol
        target_variable = target_variable or old_name
        target_symbol = target_variable
    return {
        "type": operation_type,
        "target_kind": target_kind,
        "target_method": target_method,
        "target_field": target_field,
        "target_variable": target_variable,
        "target_parameter": str(raw.get("target_parameter") or "").strip(),
        "target_symbol": target_symbol,
        "old_name": old_name,
        "target_expression": str(
            raw.get("target_expression")
            or raw.get("selected_expression")
            or raw.get("expression")
            or ""
        ).strip(),
        "selected_code": str(
            raw.get("selected_code")
            or raw.get("target_code")
            or raw.get("target_statements")
            or raw.get("selected_statements")
            or raw.get("target_expression")
            or ""
        ).strip() if operation_type == "ExtractMethod" else "",
        "target_occurrence": positive_int(
            raw.get("target_occurrence", raw.get("occurrence_index", 1)),
            default=1,
        ),
        "new_name": new_name,
        "replace_all": boolean_value(
            raw.get("replace_all", raw.get("replace_all_occurrences", False))
        ),
        "replace_duplicates": boolean_value(
            raw.get("replace_duplicates", False)
        ),
        "target_location_hint": str(raw.get("target_location_hint", "")),
    }


def operation_type_for_refactoring(refactoring: str) -> str:
    key = refactoring.lower().replace(" ", "").replace("\\", "").replace("/", "")
    if "renamemethod" in key:
        return "RenameMethod"
    if "renamefield" in key:
        return "RenameField"
    if "renamevariable" in key or "renamelocalvariable" in key:
        return "RenameVariable"
    if "inlinemethod" in key:
        return "InlineMethod"
    if "moveinstancemethod" in key:
        return "MoveInstanceMethod"
    if "extractvariable" in key or "introducevariable" in key or "extractlocalvariable" in key:
        return "ExtractVariable"
    if "extractmethod" in key or "introducemethod" in key:
        return "ExtractMethod"
    return refactoring


def validate_refactoring_operation(
    operation: Dict[str, Any],
    files: List[Dict[str, Any]],
) -> List[str]:
    operation_type = operation.get("type")
    if operation_type in {"RenameField", "RenameVariable"}:
        return validate_rename_operation(operation, files)
    if operation_type not in {"ExtractVariable", "ExtractMethod"}:
        return []
    errors: List[str] = []
    if operation_type == "ExtractMethod":
        selected_code = str(operation.get("selected_code") or "").strip()
        if not selected_code:
            errors.append("missing_extract_method_selected_code")
        name = str(operation.get("new_name") or "").strip()
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) or name in JAVA_KEYWORDS:
            errors.append("invalid_extract_method_new_name")
        if selected_code:
            occurrences = extract_target_occurrences(files, selected_code)
            if occurrences == 0:
                errors.append("extract_method_selected_code_not_found")
            elif positive_int(operation.get("target_occurrence"), default=1) > occurrences:
                errors.append("extract_method_target_occurrence_out_of_range")
        return errors
    expression = str(operation.get("target_expression") or "").strip()
    if not expression:
        errors.append("missing_extract_variable_target_expression")
    name = str(operation.get("new_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) or name in JAVA_KEYWORDS:
        errors.append("invalid_extract_variable_new_name")
    if expression:
        occurrences = extract_target_occurrences(files, expression)
        if occurrences == 0:
            errors.append("extract_variable_target_expression_not_found")
        elif positive_int(operation.get("target_occurrence"), default=1) > occurrences:
            errors.append("extract_variable_target_occurrence_out_of_range")
    return errors


def validate_rename_operation(
    operation: Dict[str, Any],
    files: List[Dict[str, Any]],
) -> List[str]:
    operation_type = str(operation.get("type") or "")
    expected_kind = "field" if operation_type == "RenameField" else "variable"
    target_key = "target_field" if expected_kind == "field" else "target_variable"
    target = str(operation.get(target_key) or operation.get("target_symbol") or "").strip()
    old_name = str(operation.get("old_name") or "").strip()
    new_name = str(operation.get("new_name") or "").strip()
    errors: List[str] = []

    if not target:
        errors.append("missing_rename_target")
    elif not is_java_identifier(target):
        errors.append("invalid_rename_target_name")
    if not is_java_identifier(new_name):
        errors.append("invalid_rename_new_name")
    if old_name and old_name != target:
        errors.append("rename_old_name_target_mismatch")
    if str(operation.get("target_kind") or "").lower() != expected_kind:
        errors.append("rename_target_kind_mismatch")

    if target:
        source = "\n".join(str(item.get("content") or "") for item in files)
        declaration_source = java_source_without_comments(source)
        declaration_found = java_variable_declaration_exists(declaration_source, target)
        if expected_kind == "field":
            declaration_found = declaration_found or java_enum_constant_declaration_exists(
                declaration_source,
                target,
            )
        if not declaration_found:
            errors.append(f"rename_{expected_kind}_declaration_not_found")
        marker = f"rename target {expected_kind}"
        if marker not in source.lower():
            errors.append("missing_rename_target_marker")
        else:
            marked_target = re.compile(
                rf"/\*\s*rename\s+target\s+{expected_kind}\s*\*/\s*{re.escape(target)}\b",
                re.IGNORECASE,
            )
            if not marked_target.search(source):
                errors.append("rename_target_marker_mismatch")
    return errors


def is_java_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value)) and value not in JAVA_KEYWORDS


def java_variable_declaration_exists(source: str, name: str) -> bool:
    identifier = re.escape(name)
    declaration = re.compile(
        rf"\b(?:var|[A-Za-z_$][A-Za-z0-9_$.]*(?:\s*<[^;{{}}()]+>)?(?:\s*\[\s*\])*)"
        rf"\s+{identifier}\b\s*(?==|;|,|:)",
        re.MULTILINE,
    )
    return bool(declaration.search(source))


def java_enum_constant_declaration_exists(source: str, name: str) -> bool:
    identifier = re.escape(name)
    enum_constant = re.compile(
        rf"\benum\b[^{{;]*\{{(?:(?!;).)*\b{identifier}\b\s*(?=\(|,|;|\}})",
        re.DOTALL,
    )
    return bool(enum_constant.search(source))


def extract_target_occurrences(files: List[Dict[str, Any]], expression: str) -> int:
    total = 0
    for item in files:
        content = str(item.get("content") or "")
        marker = content.find("Refactoring operation:")
        if marker >= 0:
            line_end = content.find("\n", marker)
            content = content[len(content) if line_end < 0 else line_end + 1:]
        total += expression_occurrences(java_source_without_comments(content), expression)
    return total


def java_source_without_comments(source: str) -> str:
    result: List[str] = []
    index = 0
    state = "code"
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code" and current == "/" and following == "/":
            result.extend("  ")
            index += 2
            state = "line_comment"
            continue
        if state == "code" and current == "/" and following == "*":
            result.extend("  ")
            index += 2
            state = "block_comment"
            continue
        if state == "line_comment":
            result.append("\n" if current == "\n" else " ")
            index += 1
            if current == "\n":
                state = "code"
            continue
        if state == "block_comment":
            if current == "*" and following == "/":
                result.extend("  ")
                index += 2
                state = "code"
            else:
                result.append("\n" if current == "\n" else " ")
                index += 1
            continue
        result.append(current)
        if current == '"' and state == "code":
            state = "string"
        elif current == "'" and state == "code":
            state = "character"
        elif current == "\\" and state in {"string", "character"} and following:
            result.append(following)
            index += 2
            continue
        elif current == '"' and state == "string":
            state = "code"
        elif current == "'" and state == "character":
            state = "code"
        index += 1
    return "".join(result)


def expression_occurrences(source: str, expression: str) -> int:
    compact = "".join(expression.split())
    if not compact:
        return 0
    pattern = r"\s*".join(re.escape(character) for character in compact)
    return sum(1 for _ in re.finditer(pattern, source))


def positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def boolean_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_oracle(raw: Any) -> Dict[str, bool]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "compile_before": bool(raw.get("compile_before", True)),
        "run_before": bool(raw.get("run_before", True)),
        "compile_after": bool(raw.get("compile_after", True)),
        "run_after": bool(raw.get("run_after", True)),
        "compare_stdout": bool(raw.get("compare_stdout", True)),
        "behavior_should_preserve": bool(raw.get("behavior_should_preserve", True)),
    }


def materialize_test_suite(test_suite: Dict[str, Any], output_root: str | Path) -> List[Path]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for case in test_suite.get("test_cases", []) or []:
        if not isinstance(case, dict):
            continue
        test_id = safe_java_identifier(str(case.get("test_id", "TestCase")))
        case_root = root / test_id
        layout = normalize_layout(
            str((test_suite.get("generation_policy", {}) or {}).get("project_layout", "simple"))
        )
        if layout == "maven":
            pom_path = case_root / "pom.xml"
            pom_path.parent.mkdir(parents=True, exist_ok=True)
            pom_path.write_text(build_maven_pom(test_id), encoding="utf-8")
            written.append(pom_path)
        for file_item in case.get("files", []) or []:
            if not isinstance(file_item, dict):
                continue
            rel = Path(str(file_item.get("path", "")))
            if rel.is_absolute() or ".." in rel.parts:
                rel = Path("src") / "main" / "java" / f"{test_id}.java"
            path = case_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(file_item.get("content", "")), encoding="utf-8")
            written.append(path)
    index_path = root / "test_cases.json"
    index_path.write_text(json.dumps(test_suite, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(index_path)
    return written


def normalize_layout(layout: str) -> str:
    normalized = str(layout or "simple").strip().lower()
    return "maven" if normalized == "maven" else "simple"


def java_source_path(class_name: str, layout: str = "simple") -> str:
    package_path = DEFAULT_TEST_PACKAGE.replace(".", "/")
    if normalize_layout(layout) == "maven":
        return f"src/main/java/{package_path}/{class_name}.java"
    return f"src/{package_path}/{class_name}.java"


def output_layout_description(layout: str) -> str:
    if normalize_layout(layout) == "maven":
        return "<output_root>/<test_id>/pom.xml and <output_root>/<test_id>/src/main/java/<test_id>.java"
    return "<output_root>/<test_id>/src/<test_id>.java"


def build_maven_pom(test_id: str) -> str:
    artifact_id = safe_java_identifier(test_id)
    return f"""<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>generated.refactoring.tests</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0-SNAPSHOT</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
</project>
"""


def validate_java_test_content(content: str) -> List[str]:
    errors: List[str] = []
    if f"package {DEFAULT_TEST_PACKAGE};" not in content:
        errors.append("missing_required_package")
    if not re.search(r"\bpublic\s+static\s+void\s+main\s*\(\s*String\s*\[\]\s+\w+\s*\)", content):
        errors.append("missing_main_method")
    if "Refactoring operation:" not in content:
        errors.append("missing_refactoring_operation_comment")
    if "System.out.print" not in content:
        errors.append("missing_deterministic_output")
    return errors


def build_empty_test_suite(
    comparison_report: Dict[str, Any],
    source_report: str,
    tests_per_scenario: int,
) -> Dict[str, Any]:
    return {
        "schema_version": TEST_CASE_SCHEMA,
        "refactoring": comparison_report.get("refactoring", ""),
        "source_comparison_report": source_report,
        "engines": comparison_report.get("engines", []),
        "generation_policy": {
            "language": "English",
            "tests_per_scenario": tests_per_scenario,
            "requires_main": True,
            "requires_refactoring_comments": True,
            "requires_behavior_output": True,
        },
        "test_cases": [],
    }


def safe_refactoring_name(refactoring: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", refactoring.strip().replace("/", "_")).strip("_")
    return safe_java_identifier(value or "Refactoring")


def safe_test_id(raw: str, refactoring: str, index: int) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip()).strip("_")
    if not value:
        value = f"{safe_refactoring_name(refactoring)}_T{index:03d}"
    return safe_java_identifier(value)


def safe_variant_category(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip().lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    return value or "unspecified"


def safe_java_identifier(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "GeneratedTest"
    if not re.match(r"[A-Za-z_]", value[0]):
        value = f"Test_{value}"
    return value


def infer_java_main_class(content: str, raw_main_class: str) -> str:
    public_class = re.search(r"\bpublic\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\b", content)
    if public_class:
        return safe_java_identifier(public_class.group(1))
    simple_name = str(raw_main_class or "").strip().split(".")[-1]
    return safe_java_identifier(simple_name)


def qualified_main_class(class_name: str) -> str:
    if "." in class_name:
        return class_name
    return f"{DEFAULT_TEST_PACKAGE}.{class_name}"


def ensure_test_package(content: str) -> str:
    text = content.strip()
    if not text:
        return text
    if re.search(r"(?m)^\s*package\s+[\w.]+\s*;", text):
        text = re.sub(
            r"(?m)^\s*package\s+[\w.]+\s*;",
            f"package {DEFAULT_TEST_PACKAGE};",
            text,
            count=1,
        )
    else:
        text = f"package {DEFAULT_TEST_PACKAGE};\n\n{text}"
    return text
