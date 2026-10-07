import difflib
import json
import os
import re
import shutil
import sys
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from refactoring_test_runner.compile_run import compile_and_run_case, not_run_result
from refactoring_test_runner.discovery import TestCase
from refactoring_test_runner.eclipse_runner import (
    compare_behavior,
    copy_case_to_workspace,
    diff_snapshots,
    mark_no_effect_success_as_failed,
    normalize_refactoring_type,
    snapshot_java_files,
)
from refactoring_test_runner.lsp_client import LspClient, wait_for


VSCODE_JAVA_SCHEMA_VERSION = "vscode_java_refactoring_run_result_v1"


class VscodeJavaJdtlsBackend:
    name = "vscode_java_jdtls"

    def __init__(
        self,
        extension_dir: str | Path,
        data_root: str | Path,
        java_path: str = "java",
        timeout: int = 180,
        startup_timeout: int = 60,
    ) -> None:
        self.extension_dir = Path(extension_dir).resolve()
        self.data_root = Path(data_root).resolve()
        self.java_path = java_path
        self.timeout = timeout
        self.startup_timeout = startup_timeout

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        operation = test_case.metadata.get("operation", {}) or {}
        refactoring = normalize_refactoring_type(str(operation.get("type") or test_case.metadata.get("refactoring") or ""))
        rename_refactoring = is_rename_refactoring(refactoring, operation)
        if not rename_refactoring and refactoring not in {"InlineMethod", "MoveInstanceMethod"}:
            return {
                "backend": self.name,
                "status": "unsupported",
                "applied": False,
                "severity": "UNKNOWN",
                "messages": [f"VS Code Java runner currently supports InlineMethod, Rename* and MoveInstanceMethod probing only, got {refactoring!r}."],
                "changed_files": [],
                "diff": "",
                "runner": {},
            }

        if not self.extension_dir.exists():
            return failed_result(f"VS Code Java extension directory not found: {self.extension_dir}")

        jdtls_script = self.extension_dir / "server" / "bin" / "jdtls"
        if not jdtls_script.exists():
            jdtls_script = self.extension_dir / "server" / "bin" / "jdtls.py"
        if not jdtls_script.exists():
            return failed_result(f"JDT LS launcher not found under extension: {self.extension_dir}")

        java_file = test_case.java_files[0] if test_case.java_files else Path("")
        if not java_file.exists():
            return failed_result("No Java file found for test case.")

        if rename_refactoring:
            target_candidates = locate_rename_targets(java_file, operation)
            target_message = "Could not locate a Rename target in the Java source."
        elif refactoring == "MoveInstanceMethod":
            target_candidates = locate_move_instance_method_targets(java_file, operation)
            target_message = "Could not locate a MoveInstanceMethod target method declaration in the Java source."
        else:
            target_candidates = locate_inline_targets(java_file, operation)
            target_message = "Could not locate an InlineMethod target call in the Java source."
        if not target_candidates:
            return {
                "backend": self.name,
                "status": "unsupported",
                "applied": False,
                "severity": "UNKNOWN",
                "messages": [target_message],
                "changed_files": [],
                "diff": "",
                "runner": {"java_file": str(java_file)},
            }
        target = target_candidates[0]

        workspace_data = self.data_root / test_case.test_id
        if workspace_data.exists():
            shutil.rmtree(workspace_data)
        workspace_data.mkdir(parents=True, exist_ok=True)

        started_at = time.time()
        applied_edits: List[Dict[str, Any]] = []
        client = LspClient(
            command=[
                sys.executable,
                str(jdtls_script),
                "--java-executable",
                self.java_path,
                "-data",
                str(workspace_data),
            ],
            cwd=test_case.case_dir,
            env={
                "JAVA_HOME": str(Path(self.java_path).resolve().parents[1])
                if Path(self.java_path).name.lower().startswith("java")
                else os.environ.get("JAVA_HOME", ""),
            },
            apply_edit_handler=lambda edit: apply_workspace_edit(edit, applied_edits),
        )

        try:
            client.start()
            root_uri = path_to_uri(test_case.case_dir)
            java_uri = path_to_uri(java_file)
            initialize_result = client.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root_uri,
                    "rootPath": str(test_case.case_dir),
                    "workspaceFolders": [{"uri": root_uri, "name": test_case.test_id}],
                    "capabilities": client_capabilities(),
                    "initializationOptions": {
                        "settings": {
                            "java": {
                                "project": {"sourcePaths": ["src"], "outputPath": "bin"},
                                "autobuild": {"enabled": False},
                                "import": {"maven": {"enabled": False}, "gradle": {"enabled": False}},
                                "errors": {"incompleteClasspath": {"severity": "ignore"}},
                            }
                        }
                    },
                },
                timeout=self.startup_timeout,
            )
            client.notify("initialized", {})
            text = java_file.read_text(encoding="utf-8", errors="replace")
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": java_uri,
                        "languageId": "java",
                        "version": 1,
                        "text": text,
                    }
                },
            )
            wait_for(lambda: any((n.get("method") == "textDocument/publishDiagnostics") for n in client.notifications), timeout=15)

            if rename_refactoring:
                return apply_rename(
                    client=client,
                    java_uri=java_uri,
                    target_candidates=target_candidates,
                    operation=operation,
                    initialize_result=initialize_result,
                    applied_edits=applied_edits,
                    started_at=started_at,
                )
            if refactoring == "MoveInstanceMethod":
                return apply_move_instance_method_action(
                    client=client,
                    java_uri=java_uri,
                    target_candidates=target_candidates,
                    initialize_result=initialize_result,
                    applied_edits=applied_edits,
                    started_at=started_at,
                )

            action = None
            actions: Any = None
            tried_targets = []
            for candidate in target_candidates:
                target = candidate
                tried_targets.append(candidate)
                ranges = [
                    {
                        "start": {"line": candidate[0], "character": candidate[1]},
                        "end": {"line": candidate[0], "character": candidate[1]},
                    },
                    {
                        "start": {"line": candidate[0], "character": candidate[1]},
                        "end": {"line": candidate[0], "character": candidate[2]},
                    },
                ]
                for action_range in ranges:
                    actions = client.request(
                        "textDocument/codeAction",
                        {
                            "textDocument": {"uri": java_uri},
                            "range": action_range,
                            "context": {"diagnostics": [], "only": ["refactor.inline", "refactor"], "triggerKind": 1},
                        },
                        timeout=self.timeout,
                    )
                    action = select_inline_action(actions)
                    if action is not None:
                        break
                if action is not None:
                    break

            if action is None:
                titles = [str(item.get("title") or item.get("command", {}).get("title") or "") for item in (actions or []) if isinstance(item, dict)]
                return rejected_result(
                    "JDT LS returned no matching Inline Method action. Titles: " + "; ".join(titles),
                    target,
                    initialize_result,
                    client,
                    applied_edits,
                    extra_runner={"tried_targets": tried_targets},
                )

            if action.get("data") and not action.get("edit") and not action.get("command"):
                action = client.request("codeAction/resolve", action, timeout=self.timeout)

            applied = execute_code_action(client, action, applied_edits)
            if not applied:
                return failed_result(
                    "Inline Method code action was selected but no workspace edit was applied.",
                    runner_payload(target, initialize_result, client, applied_edits, action),
                )

            return {
                "backend": self.name,
                "status": "success",
                "applied": True,
                "severity": "OK",
                "messages": [f"Applied VS Code Java/JDT LS code action: {action_title(action)}"],
                "changed_files": [],
                "diff": "",
                "runner": runner_payload(target, initialize_result, client, applied_edits, action, started_at),
            }
        except TimeoutError as exc:
            return {
                "backend": self.name,
                "status": "timeout",
                "applied": False,
                "severity": "ERROR",
                "messages": [str(exc)],
                "changed_files": [],
                "diff": "",
                "runner": {"target": target, "duration_ms": int((time.time() - started_at) * 1000)},
            }
        except Exception as exc:
            return failed_result(str(exc), {"target": target, "duration_ms": int((time.time() - started_at) * 1000)})
        finally:
            try:
                client.request("shutdown", None, timeout=5)
            except Exception:
                pass
            client.stop()


class VscodeJavaRefactoringRunner:
    def __init__(
        self,
        backend: Any,
        java_path: str = "java",
        javac_path: str = "javac",
        compile_timeout: int = 10,
        run_timeout: int = 5,
    ) -> None:
        self.backend = backend
        self.java_path = java_path
        self.javac_path = javac_path
        self.compile_timeout = compile_timeout
        self.run_timeout = run_timeout

    def run_case(self, test_case: TestCase, workspace_root: str | Path) -> Dict[str, Any]:
        started_at = time.time()
        workspace_case = copy_case_to_workspace(test_case, workspace_root)
        before = compile_and_run_case(
            workspace_case,
            java_path=self.java_path,
            javac_path=self.javac_path,
            compile_timeout=self.compile_timeout,
            run_timeout=self.run_timeout,
        )

        if before["compile"]["status"] != "ok":
            refactoring = {
                "backend": backend_name(self.backend),
                "status": "skipped",
                "applied": False,
                "severity": "UNKNOWN",
                "messages": ["Before-refactoring compilation failed; VS Code Java refactoring was skipped."],
                "changed_files": [],
                "diff": "",
                "runner": {},
            }
            after = not_run_after("before_compile_failed")
            behavior_preserved = None
        else:
            snapshot_before = snapshot_java_files(workspace_case.case_dir)
            refactoring = normalize_refactoring_result(self.backend.apply(workspace_case), backend_name(self.backend))
            snapshot_after = snapshot_java_files(workspace_case.case_dir)
            changed_files, diff = diff_snapshots(snapshot_before, snapshot_after)
            if changed_files and not refactoring.get("changed_files"):
                refactoring["changed_files"] = changed_files
            if diff and not refactoring.get("diff"):
                refactoring["diff"] = diff
            mark_no_effect_success_as_failed(refactoring, changed_files, diff)

            if refactoring.get("applied"):
                after = compile_and_run_case(
                    workspace_case,
                    java_path=self.java_path,
                    javac_path=self.javac_path,
                    compile_timeout=self.compile_timeout,
                    run_timeout=self.run_timeout,
                )
                behavior_preserved = compare_behavior(before, after)
            else:
                after = not_run_after("refactoring_not_applied")
                behavior_preserved = None

        return {
            "schema_version": VSCODE_JAVA_SCHEMA_VERSION,
            "engine": "VS Code Java",
            "test_id": test_case.test_id,
            "refactoring_name": str(test_case.metadata.get("refactoring") or ""),
            "operation": test_case.metadata.get("operation", {}) or {},
            "source_case_dir": str(test_case.case_dir),
            "workspace_case_dir": str(workspace_case.case_dir),
            "before": before,
            "refactoring": refactoring,
            "refactoring_status": refactoring.get("status"),
            "after": after,
            "behavior_preserved": behavior_preserved,
            "duration_ms": int((time.time() - started_at) * 1000),
        }


def client_capabilities() -> Dict[str, Any]:
    return {
        "workspace": {
            "applyEdit": True,
            "workspaceEdit": {"documentChanges": True, "resourceOperations": ["create", "rename", "delete"]},
            "configuration": True,
            "executeCommand": {"dynamicRegistration": True},
            "workspaceFolders": True,
        },
        "textDocument": {
            "synchronization": {"didSave": True, "dynamicRegistration": True},
            "codeAction": {
                "dynamicRegistration": True,
                "codeActionLiteralSupport": {
                    "codeActionKind": {
                        "valueSet": ["", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite"]
                    }
                },
                "resolveSupport": {"properties": ["edit", "command"]},
            },
            "publishDiagnostics": {"relatedInformation": True},
            "rename": {"dynamicRegistration": True, "prepareSupport": True},
        },
    }


def locate_inline_targets(java_file: Path, operation: Dict[str, Any]) -> List[Tuple[int, int, int]]:
    text = java_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    target = method_name(operation.get("target_method") or operation.get("target_symbol") or "")
    if not target:
        target = method_name(operation.get("target_name") or "")
    if not target:
        return []

    candidates: List[Tuple[int, int, int]] = []

    comment_lines = [
        index for index, line in enumerate(lines)
        if "inline" in line.lower() and (target.lower() in line.lower() or "call" in line.lower() or "target" in line.lower())
    ]
    for index in comment_lines:
        for line_index in range(index, min(index + 4, len(lines))):
            match = call_match(lines[line_index], target)
            if match and not is_likely_declaration(lines[line_index], match.start()):
                candidates.append((line_index, match.start(), match.end()))
            match = method_reference_match(lines[line_index], target)
            if match:
                candidates.append((line_index, match.start(), match.end()))

    for line_index, line in enumerate(lines):
        match = call_match(line, target)
        if match and not is_likely_declaration(line, match.start()):
            candidates.append((line_index, match.start(), match.end()))
        match = method_reference_match(line, target)
        if match:
            candidates.append((line_index, match.start(), match.end()))

    return dedupe_targets(candidates)


def locate_rename_targets(java_file: Path, operation: Dict[str, Any]) -> List[Tuple[int, int, int]]:
    text = java_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    target = rename_target_name(operation)
    if not target:
        return []

    kind = rename_kind(operation)
    candidates: List[Tuple[int, int, int]] = []
    comment_lines = [
        index for index, line in enumerate(lines)
        if ("target" in line.lower() or "rename" in line.lower()) and target.lower() in line.lower()
    ]
    comment_lines.extend(
        index for index, line in enumerate(lines)
        if "target method" in line.lower() or "target field" in line.lower() or "target class" in line.lower()
    )
    for index in comment_lines:
        for line_index in range(index, min(index + 5, len(lines))):
            candidates.extend(rename_line_candidates(lines[line_index], target, kind, line_index))

    for line_index, line in enumerate(lines):
        candidates.extend(rename_line_candidates(line, target, kind, line_index))

    if kind == "method":
        for line_index, line in enumerate(lines):
            match = call_match(line, target)
            if match and not is_likely_declaration(line, match.start()):
                candidates.append((line_index, match.start(), match.end()))
            match = method_reference_match(line, target)
            if match:
                candidates.append((line_index, match.start(), match.end()))
    else:
        for line_index, line in enumerate(lines):
            match = identifier_match(line, target)
            if match:
                candidates.append((line_index, match.start(), match.end()))

    return dedupe_targets(candidates)


def is_rename_refactoring(refactoring: str, operation: Dict[str, Any]) -> bool:
    key = str(refactoring or operation.get("type") or "").strip().lower().replace(" ", "").replace("\\", "").replace("/", "")
    return key.startswith("rename") or "rename" in key


def rename_kind(operation: Dict[str, Any]) -> str:
    raw = str(operation.get("type") or operation.get("refactoring") or "").lower()
    if "method" in raw:
        return "method"
    if "field" in raw:
        return "field"
    if "class" in raw or "type" in raw:
        return "class"
    if "parameter" in raw:
        return "parameter"
    if "variable" in raw or "local" in raw:
        return "variable"
    if operation.get("target_method"):
        return "method"
    if operation.get("target_field"):
        return "field"
    if operation.get("target_class") or operation.get("target_type"):
        return "class"
    if operation.get("target_parameter"):
        return "parameter"
    if operation.get("target_variable"):
        return "variable"
    return "symbol"


def rename_target_name(operation: Dict[str, Any]) -> str:
    value = (
        operation.get("target_method")
        or operation.get("target_field")
        or operation.get("target_class")
        or operation.get("target_type")
        or operation.get("target_parameter")
        or operation.get("target_variable")
        or operation.get("target_symbol")
        or operation.get("old_name")
        or operation.get("oldName")
        or operation.get("target_name")
        or ""
    )
    return method_name(value)


def rename_new_name(operation: Dict[str, Any], old_name: str = "") -> str:
    value = (
        operation.get("new_name")
        or operation.get("newName")
        or operation.get("new_symbol")
        or operation.get("newSymbol")
        or operation.get("replacement_name")
        or operation.get("replacement")
        or ""
    )
    candidate = method_name(value)
    if not candidate:
        fallback = method_name(operation.get("target_name") or "")
        if fallback and fallback != old_name:
            candidate = fallback
    return candidate


def rename_line_candidates(line: str, target: str, kind: str, line_index: int) -> List[Tuple[int, int, int]]:
    matchers = []
    if kind == "method":
        matchers = [method_declaration_name_match]
    elif kind == "class":
        matchers = [type_declaration_name_match]
    elif kind == "field":
        matchers = [field_declaration_name_match]
    elif kind in {"parameter", "variable"}:
        matchers = [variable_declaration_name_match]
    else:
        matchers = [method_declaration_name_match, type_declaration_name_match, field_declaration_name_match, variable_declaration_name_match]

    result: List[Tuple[int, int, int]] = []
    for matcher in matchers:
        match = matcher(line, target)
        if match:
            result.append((line_index, match.start(), match.end()))
    return result


def locate_move_instance_method_targets(java_file: Path, operation: Dict[str, Any]) -> List[Tuple[int, int, int]]:
    text = java_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    target = method_name(operation.get("target_method") or operation.get("target_symbol") or operation.get("target_name") or "")
    if not target:
        return []

    candidates: List[Tuple[int, int, int]] = []
    comment_lines = [
        index for index, line in enumerate(lines)
        if ("target method" in line.lower() or "move" in line.lower()) and target.lower() in line.lower()
    ]
    for index in comment_lines:
        for line_index in range(index, min(index + 6, len(lines))):
            match = method_declaration_name_match(lines[line_index], target)
            if match:
                candidates.append((line_index, match.start(), match.end()))

    for line_index, line in enumerate(lines):
        match = method_declaration_name_match(line, target)
        if match:
            candidates.append((line_index, match.start(), match.end()))

    return dedupe_targets(candidates)


def method_declaration_name_match(line: str, target: str) -> Optional[re.Match[str]]:
    comment_start = line.find("//")
    pattern = re.compile(rf"\b{re.escape(target)}\s*\(")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start() >= comment_start:
            continue
        if is_likely_declaration(line, match.start()):
            return match
    return None


def type_declaration_name_match(line: str, target: str) -> Optional[re.Match[str]]:
    comment_start = line.find("//")
    pattern = re.compile(rf"\b(?:class|interface|enum|record)\s+({re.escape(target)})\b")
    match = pattern.search(line)
    if match and not (comment_start >= 0 and match.start(1) >= comment_start):
        return _GroupAsMatch(match, 1)
    return None


def field_declaration_name_match(line: str, target: str) -> Optional[re.Match[str]]:
    comment_start = line.find("//")
    if "(" in line[: line.find(target) if target in line else len(line)]:
        return None
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start() >= comment_start:
            continue
        prefix = line[: match.start()]
        suffix = line[match.end():]
        if re.search(r"(?:^|[;{}]\s*)(?:public|private|protected|static|final|volatile|transient|\s)*[\w<>\[\].?,]+\s+$", prefix) and re.match(r"\s*(?:=|;|,)", suffix):
            return match
    return None


def variable_declaration_name_match(line: str, target: str) -> Optional[re.Match[str]]:
    comment_start = line.find("//")
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start() >= comment_start:
            continue
        prefix = line[: match.start()]
        suffix = line[match.end():]
        if re.search(r"(?:^|[({,]\s*)(?:final\s+)?[\w<>\[\].?,]+\s+$", prefix) and re.match(r"\s*(?:=|,|\)|;)", suffix):
            return match
    return None


def method_name(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"([A-Za-z_$][\w$]*)\s*\(", text)
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z_$][\w$]*)", text)
    return match.group(1) if match else ""


def call_match(line: str, target: str) -> Optional[re.Match[str]]:
    pattern = re.compile(rf"\b{re.escape(target)}\s*\(")
    comment_start = line.find("//")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start() >= comment_start:
            continue
        return match
    return None


def method_reference_match(line: str, target: str) -> Optional[re.Match[str]]:
    pattern = re.compile(rf"::\s*({re.escape(target)})\b")
    comment_start = line.find("//")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start(1) >= comment_start:
            continue
        return _GroupAsMatch(match, 1)
    return None


def identifier_match(line: str, target: str) -> Optional[re.Match[str]]:
    comment_start = line.find("//")
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    for match in pattern.finditer(line):
        if comment_start >= 0 and match.start() >= comment_start:
            continue
        return match
    return None


class _GroupAsMatch:
    def __init__(self, match: re.Match[str], group_index: int) -> None:
        self.match = match
        self.group_index = group_index

    def start(self) -> int:
        return self.match.start(self.group_index)

    def end(self) -> int:
        return self.match.end(self.group_index)


def dedupe_targets(targets: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    seen = set()
    result: List[Tuple[int, int, int]] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        result.append(target)
    return result


def is_likely_declaration(line: str, start: int) -> bool:
    raw_prefix = line[:start]
    prefix = raw_prefix.strip()
    if prefix.endswith(".") or prefix.endswith("::") or prefix.endswith("new"):
        return False
    declaration_prefixes = ("public ", "private ", "protected ", "static ", "final ", "native ", "abstract ", "synchronized ")
    if any(part in prefix.split() for part in ["=", "return", "throw"]):
        return False
    if re.search(r"(?:^|\s)(?:void|boolean|byte|short|int|long|float|double|char|[A-Za-z_$][\w$]*(?:\s*<[^>]+>)?(?:\s*\[\])*)\s*$", raw_prefix):
        return True
    return bool(prefix) and (
        any(prefix.startswith(item.strip()) for item in declaration_prefixes)
        or re.search(r"[\w<>\[\].?,]+\s+$", raw_prefix) is not None
    )


def select_inline_action(actions: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(actions, list):
        return None
    for item in actions:
        if not isinstance(item, dict):
            continue
        title = action_title(item).lower()
        kind = str(item.get("kind") or "").lower()
        command = item.get("command") or {}
        command_text = str(command.get("command") if isinstance(command, dict) else command).lower()
        if "inline" in title and ("method" in title or "refactor.inline" in kind or "inline" in command_text):
            return item
    for item in actions:
        if isinstance(item, dict) and "inline" in action_title(item).lower():
            return item
    return None


def apply_rename(
    client: LspClient,
    java_uri: str,
    target_candidates: List[Tuple[int, int, int]],
    operation: Dict[str, Any],
    initialize_result: Any,
    applied_edits: List[Dict[str, Any]],
    started_at: float,
) -> Dict[str, Any]:
    old_name = rename_target_name(operation)
    new_name = rename_new_name(operation, old_name)
    if not new_name:
        return {
            "backend": "vscode_java_jdtls",
            "status": "unsupported",
            "applied": False,
            "severity": "UNKNOWN",
            "messages": ["Rename operation does not provide new_name/newName."],
            "changed_files": [],
            "diff": "",
            "runner": {"target_candidates": target_candidates},
        }

    errors: List[str] = []
    for target in target_candidates:
        position = {"line": target[0], "character": target[1]}
        try:
            prepare = client.request(
                "textDocument/prepareRename",
                {"textDocument": {"uri": java_uri}, "position": position},
                timeout=60,
            )
        except RuntimeError as exc:
            errors.append(f"prepareRename at {target}: {exc}")
            continue
        except TimeoutError:
            raise

        if prepare is None:
            errors.append(f"prepareRename at {target}: null response")
            continue

        try:
            edit = client.request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": java_uri},
                    "position": position,
                    "newName": new_name,
                },
                timeout=120,
            )
        except RuntimeError as exc:
            errors.append(f"rename at {target}: {exc}")
            continue
        except TimeoutError:
            raise

        if not isinstance(edit, dict):
            errors.append(f"rename at {target}: no workspace edit returned")
            continue
        if apply_workspace_edit(edit, applied_edits):
            return {
                "backend": "vscode_java_jdtls",
                "status": "success",
                "applied": True,
                "severity": "OK",
                "messages": [f"Applied VS Code Java/JDT LS rename to {new_name}."],
                "changed_files": [],
                "diff": "",
                "runner": {
                    "target_line": target[0],
                    "target_start_character": target[1],
                    "target_end_character": target[2],
                    "old_name": old_name,
                    "new_name": new_name,
                    "prepare_rename": prepare,
                    "applied_edits": applied_edits,
                    "server_capabilities": (initialize_result or {}).get("capabilities", {}) if isinstance(initialize_result, dict) else {},
                    "duration_ms": int((time.time() - started_at) * 1000),
                },
            }
        errors.append(f"rename at {target}: workspace edit was empty or could not be applied")

    return {
        "backend": "vscode_java_jdtls",
        "status": "rejected",
        "applied": False,
        "severity": "ERROR",
        "messages": errors or ["JDT LS rejected rename."],
        "changed_files": [],
        "diff": "",
        "runner": {
            "target_candidates": target_candidates,
            "old_name": old_name,
            "new_name": new_name,
            "notifications_seen": [str(item.get("method") or "") for item in client.notifications[-20:]],
            "requests_seen": [str(item.get("method") or "") for item in client.requests[-20:]],
            "duration_ms": int((time.time() - started_at) * 1000),
        },
    }


def apply_move_instance_method_action(
    client: LspClient,
    java_uri: str,
    target_candidates: List[Tuple[int, int, int]],
    initialize_result: Any,
    applied_edits: List[Dict[str, Any]],
    started_at: float,
) -> Dict[str, Any]:
    all_titles: List[str] = []
    tried_targets: List[Tuple[int, int, int]] = []
    selected_target: Optional[Tuple[int, int, int]] = None
    selected_action: Optional[Dict[str, Any]] = None

    for target in target_candidates:
        tried_targets.append(target)
        ranges = [
            {
                "start": {"line": target[0], "character": target[1]},
                "end": {"line": target[0], "character": target[1]},
            },
            {
                "start": {"line": target[0], "character": target[1]},
                "end": {"line": target[0], "character": target[2]},
            },
        ]
        for action_range in ranges:
            actions = client.request(
                "textDocument/codeAction",
                {
                    "textDocument": {"uri": java_uri},
                    "range": action_range,
                    "context": {"diagnostics": [], "only": ["refactor.move", "refactor"], "triggerKind": 1},
                },
                timeout=120,
            )
            if isinstance(actions, list):
                all_titles.extend(action_title(item) for item in actions if isinstance(item, dict))
            action = select_move_instance_method_action(actions)
            if action is not None:
                selected_target = target
                selected_action = action
                break
        if selected_action is not None:
            break

    if selected_action is None or selected_target is None:
        return {
            "backend": "vscode_java_jdtls",
            "status": "unsupported",
            "applied": False,
            "severity": "UNKNOWN",
            "messages": [
                "JDT LS returned no Move Instance Method / Move Method code action. "
                + "Titles: " + "; ".join(title for title in all_titles if title)
            ],
            "changed_files": [],
            "diff": "",
            "runner": {
                "target_candidates": target_candidates,
                "tried_targets": tried_targets,
                "server_capabilities": (initialize_result or {}).get("capabilities", {}) if isinstance(initialize_result, dict) else {},
                "duration_ms": int((time.time() - started_at) * 1000),
            },
        }

    if selected_action.get("data") and not selected_action.get("edit") and not selected_action.get("command"):
        selected_action = client.request("codeAction/resolve", selected_action, timeout=120)

    applied = execute_code_action(client, selected_action, applied_edits)
    if not applied:
        return {
            "backend": "vscode_java_jdtls",
            "status": "failed",
            "applied": False,
            "severity": "ERROR",
            "messages": ["Move code action was selected but no workspace edit was applied."],
            "changed_files": [],
            "diff": "",
            "runner": runner_payload(selected_target, initialize_result, client, applied_edits, selected_action, started_at),
        }

    return {
        "backend": "vscode_java_jdtls",
        "status": "success",
        "applied": True,
        "severity": "OK",
        "messages": [f"Applied VS Code Java/JDT LS code action: {action_title(selected_action)}"],
        "changed_files": [],
        "diff": "",
        "runner": runner_payload(selected_target, initialize_result, client, applied_edits, selected_action, started_at),
    }


def select_move_instance_method_action(actions: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(actions, list):
        return None
    for item in actions:
        if not isinstance(item, dict):
            continue
        title = action_title(item).lower()
        kind = str(item.get("kind") or "").lower()
        command = item.get("command") or {}
        command_text = str(command.get("command") if isinstance(command, dict) else command).lower()
        combined = " ".join([title, kind, command_text])
        if "move" in combined and ("method" in combined or "member" in combined or "refactor.move" in combined):
            return item
    return None


def execute_code_action(client: LspClient, action: Dict[str, Any], applied_edits: List[Dict[str, Any]]) -> bool:
    if action.get("edit"):
        return apply_workspace_edit(action["edit"], applied_edits)
    command = action.get("command")
    if not isinstance(command, dict):
        return False
    command_id = str(command.get("command") or "")
    args = command.get("arguments") or []
    if "apply.workspaceedit" in command_id.replace("_", "").replace("-", "").lower():
        for arg in args:
            if isinstance(arg, dict) and ("changes" in arg or "documentChanges" in arg):
                return apply_workspace_edit(arg, applied_edits)
    result = client.request("workspace/executeCommand", {"command": command_id, "arguments": args}, timeout=120)
    if isinstance(result, dict) and ("changes" in result or "documentChanges" in result):
        return apply_workspace_edit(result, applied_edits)
    return bool(applied_edits)


def apply_workspace_edit(edit: Dict[str, Any], applied_edits: List[Dict[str, Any]]) -> bool:
    changed = False
    document_changes = edit.get("documentChanges") if isinstance(edit, dict) else None
    if isinstance(document_changes, list):
        for change in document_changes:
            if not isinstance(change, dict):
                continue
            if "edits" in change and "textDocument" in change:
                uri = (change.get("textDocument") or {}).get("uri")
                changed = apply_text_edits(uri, change.get("edits") or [], applied_edits) or changed
    changes = edit.get("changes") if isinstance(edit, dict) else None
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            changed = apply_text_edits(uri, edits or [], applied_edits) or changed
    return changed


def apply_text_edits(uri: str, edits: List[Dict[str, Any]], applied_edits: List[Dict[str, Any]]) -> bool:
    if not uri or not edits:
        return False
    path = uri_to_path(uri)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    line_offsets = compute_line_offsets(text)
    sorted_edits = sorted(edits, key=lambda item: offset_for_range(item.get("range") or {}, line_offsets), reverse=True)
    for edit in sorted_edits:
        range_obj = edit.get("range") or {}
        start = offset_for_position(range_obj.get("start") or {}, line_offsets)
        end = offset_for_position(range_obj.get("end") or {}, line_offsets)
        text = text[:start] + str(edit.get("newText") or "") + text[end:]
    path.write_text(text, encoding="utf-8")
    applied_edits.append({"uri": uri, "path": str(path), "edits": len(edits)})
    return True


def compute_line_offsets(text: str) -> List[int]:
    offsets = [0]
    for match in re.finditer(r"\n", text):
        offsets.append(match.end())
    return offsets


def offset_for_range(range_obj: Dict[str, Any], line_offsets: List[int]) -> int:
    return offset_for_position(range_obj.get("start") or {}, line_offsets)


def offset_for_position(position: Dict[str, Any], line_offsets: List[int]) -> int:
    line = int(position.get("line") or 0)
    character = int(position.get("character") or 0)
    if line < 0:
        line = 0
    if line >= len(line_offsets):
        return line_offsets[-1]
    return line_offsets[line] + character


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return Path(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return Path(path)


def action_title(action: Dict[str, Any]) -> str:
    title = str(action.get("title") or "")
    command = action.get("command")
    if not title and isinstance(command, dict):
        title = str(command.get("title") or command.get("command") or "")
    return title


def runner_payload(
    target: Tuple[int, int, int],
    initialize_result: Any,
    client: LspClient,
    applied_edits: List[Dict[str, Any]],
    action: Optional[Dict[str, Any]] = None,
    started_at: Optional[float] = None,
) -> Dict[str, Any]:
    payload = {
        "target_line": target[0],
        "target_start_character": target[1],
        "target_end_character": target[2],
        "applied_edits": applied_edits,
        "server_capabilities": (initialize_result or {}).get("capabilities", {}) if isinstance(initialize_result, dict) else {},
        "notifications_seen": [str(item.get("method") or "") for item in client.notifications[-20:]],
        "requests_seen": [str(item.get("method") or "") for item in client.requests[-20:]],
    }
    if action is not None:
        payload["selected_action_title"] = action_title(action)
        payload["selected_action_kind"] = str(action.get("kind") or "")
        payload["selected_action_command"] = str((action.get("command") or {}).get("command") or "") if isinstance(action.get("command"), dict) else ""
    if started_at is not None:
        payload["duration_ms"] = int((time.time() - started_at) * 1000)
    return payload


def normalize_refactoring_result(raw: Dict[str, Any], backend: str) -> Dict[str, Any]:
    status = str(raw.get("status") or "failed").strip().lower()
    if status not in {"success", "rejected", "warning", "failed", "unsupported", "timeout", "skipped"}:
        status = "failed"
    applied = bool(raw.get("applied", status in {"success", "warning"}))
    severity = str(raw.get("severity") or severity_from_status(status)).strip().upper()
    messages = raw.get("messages") or []
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        messages = [str(messages)]
    return {
        "backend": str(raw.get("backend") or backend),
        "status": status,
        "applied": applied,
        "severity": severity,
        "messages": [str(item) for item in messages],
        "changed_files": [str(item) for item in (raw.get("changed_files") or [])],
        "diff": str(raw.get("diff") or ""),
        "runner": raw.get("runner") or {},
    }


def severity_from_status(status: str) -> str:
    if status == "success":
        return "OK"
    if status == "warning":
        return "WARNING"
    if status in {"rejected", "failed", "timeout"}:
        return "ERROR"
    return "UNKNOWN"


def not_run_after(reason: str) -> Dict[str, Any]:
    return {
        "status": "not_run",
        "reason": reason,
        "compile": not_run_result(reason),
        "run": not_run_result(reason),
    }


def backend_name(backend: Any) -> str:
    return str(getattr(backend, "name", backend.__class__.__name__))


def failed_result(message: str, runner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "backend": "vscode_java_jdtls",
        "status": "failed",
        "applied": False,
        "severity": "ERROR",
        "messages": [message],
        "changed_files": [],
        "diff": "",
        "runner": runner or {},
    }


def rejected_result(
    message: str,
    target: Tuple[int, int, int],
    initialize_result: Any,
    client: LspClient,
    applied_edits: List[Dict[str, Any]],
    extra_runner: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runner = runner_payload(target, initialize_result, client, applied_edits)
    if extra_runner:
        runner.update(extra_runner)
    return {
        "backend": "vscode_java_jdtls",
        "status": "rejected",
        "applied": False,
        "severity": "ERROR",
        "messages": [message],
        "changed_files": [],
        "diff": "",
        "runner": runner,
    }


def find_vscode_java_extension() -> Optional[Path]:
    env_path = os.environ.get("VSCODE_JAVA_EXTENSION")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    base = Path(os.environ.get("USERPROFILE", "")) / ".vscode" / "extensions"
    if not base.exists():
        return None
    candidates = sorted(base.glob("redhat.java-*"), key=lambda item: item.name.lower(), reverse=True)
    return candidates[0] if candidates else None
