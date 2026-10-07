import difflib
import json
import shutil
import time

from pathlib import Path
from typing import Any, Dict, List, Sequence

from refactoring_test_runner.compile_run import compile_and_run_case, not_run_result, run_command
from refactoring_test_runner.discovery import TestCase
from refactoring_test_runner.extract_method import extract_method_options
from refactoring_test_runner.extract_variable import extract_variable_options
from refactoring_test_runner.inline_method import inline_method_options


ECLIPSE_SCHEMA_VERSION = "eclipse_refactoring_run_result_v1"


def normalize_refactoring_type(value: str) -> str:
    key = str(value).strip().lower().replace(" ", "").replace("\\", "").replace("/", "")
    if "renamemethod" in key:
        return "RenameMethod"
    if "renamefield" in key:
        return "RenameField"
    if "renamevariable" in key or "renamelocalvariable" in key or "renamelocal" in key:
        return "RenameVariable"
    if "renameparameter" in key:
        return "RenameVariable"
    if "inlinemethod" in key:
        return "InlineMethod"
    if "moveinstancemethod" in key:
        return "MoveInstanceMethod"
    if "extractvariable" in key or "introducevariable" in key or "extractlocalvariable" in key:
        return "ExtractVariable"
    if "extractmethod" in key or "introducemethod" in key:
        return "ExtractMethod"
    return str(value).strip()


def _operation_options(
    test_case: TestCase,
    operation: Dict[str, Any],
    refactoring: str,
    *,
    preserve_newlines: bool,
) -> Dict[str, Any]:
    if refactoring == "ExtractVariable":
        return extract_variable_options(test_case, operation, preserve_newlines=preserve_newlines)
    if refactoring == "ExtractMethod":
        return extract_method_options(test_case, operation, preserve_newlines=preserve_newlines)
    if refactoring == "InlineMethod":
        return inline_method_options(test_case, operation, preserve_newlines=preserve_newlines)
    return {}


def rename_target_kind(refactoring: str, operation: Dict[str, Any]) -> str:
    key = str(refactoring or operation.get("type") or "").strip().lower().replace(" ", "").replace("\\", "").replace("/", "")
    if "method" in key:
        return "method"
    if "field" in key:
        return "field"
    if "parameter" in key:
        return "variable"
    if "variable" in key or "local" in key:
        return "variable"
    if operation.get("target_method"):
        return "method"
    if operation.get("target_field"):
        return "field"
    if operation.get("target_parameter") or operation.get("target_variable"):
        return "variable"
    return ""


def rename_target_symbol(operation: Dict[str, Any]) -> str:
    return str(
        operation.get("target_method")
        or operation.get("target_field")
        or operation.get("target_parameter")
        or operation.get("target_variable")
        or operation.get("target_symbol")
        or operation.get("target_name")
        or ""
    )


class UnsupportedEclipseBackend:
    name = "not_configured"

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "status": "unsupported",
            "applied": False,
            "severity": "UNKNOWN",
            "messages": [
                "No Eclipse refactoring backend is configured. "
                "Compile/run was executed, but the Eclipse refactoring step was skipped."
            ],
            "changed_files": [],
            "diff": "",
            "runner": {},
        }


class CommandEclipseBackend:
    name = "command"

    def __init__(self, command: Sequence[str], timeout: int = 60) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = list(command)
        self.timeout = timeout

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        output_path = test_case.case_dir / ".eclipse_refactoring_result.json"
        if output_path.exists():
            output_path.unlink()

        operation = test_case.metadata.get("operation", {}) or {}
        refactoring = normalize_refactoring_type(str(operation.get("type") or test_case.metadata.get("refactoring") or ""))
        target_method = str(operation.get("target_method") or operation.get("target_symbol") or "")
        is_rename_refactoring = refactoring.lower().startswith("rename")
        target_kind = rename_target_kind(refactoring, operation) if is_rename_refactoring else str(operation.get("target_kind") or "")
        target_symbol = str(operation.get("target_symbol") or "")
        if is_rename_refactoring:
            rename_target = rename_target_symbol(operation)
            if rename_target:
                target_symbol = rename_target
        target_name = str(operation.get("target_name") or "")
        target_hint = str(operation.get("target_location_hint") or "")
        new_name = str(operation.get("new_name") or operation.get("target_name") or "")
        operation_options = _operation_options(test_case, operation, refactoring, preserve_newlines=True)
        if refactoring in {"ExtractVariable", "ExtractMethod"} and operation_options:
            new_name = str(operation_options["new_name"])
        java_file = test_case.java_files[0] if test_case.java_files else Path("")
        command = self.command + [
            "--case-dir",
            str(test_case.case_dir),
            "--java-file",
            str(java_file),
            "--refactoring",
            refactoring,
            "--target-method",
            target_method,
        ]
        if target_symbol:
            command.extend(["--target-symbol", target_symbol])
        if target_kind:
            command.extend(["--target-kind", target_kind])
        if target_name:
            command.extend(["--target-name", target_name])
        if target_hint:
            command.extend(["--target-hint", target_hint])
        if new_name:
            command.extend(["--new-name", new_name])
        if operation_options:
            command.extend(["--target-start", str(operation_options["target_start"])])
            command.extend(["--target-length", str(operation_options["target_length"])])
            if refactoring == "ExtractVariable":
                command.extend(["--replace-all", str(operation_options["replace_all"]).lower()])
            elif refactoring == "ExtractMethod":
                command.extend(["--replace-duplicates", str(operation_options["replace_duplicates"]).lower()])
        command.extend(["--output", str(output_path)])
        runner = run_command(command, cwd=test_case.case_dir, timeout=self.timeout)

        if output_path.exists():
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raw = {
                    "status": "failed",
                    "applied": False,
                    "severity": "ERROR",
                    "messages": [f"Backend wrote invalid JSON: {exc}"],
                }
        else:
            raw = {
                "status": "failed" if runner["status"] != "timeout" else "timeout",
                "applied": False,
                "severity": "ERROR",
                "messages": ["Backend did not write a refactoring result JSON file."],
            }

        result = normalize_refactoring_result(raw, backend=self.name)
        result["runner"] = runner
        return result


class EclipseApplicationBackend:
    name = "eclipse_application"

    def __init__(
        self,
        eclipse_home: str | Path,
        configuration: str | Path,
        data_root: str | Path,
        application_id: str = "experiment.eclipse.refactoring.backend.application",
        vm_path: str = "",
        timeout: int = 120,
    ) -> None:
        self.eclipse_home = Path(eclipse_home).resolve()
        self.configuration = Path(configuration).resolve()
        self.data_root = Path(data_root).resolve()
        self.application_id = application_id
        self.vm_path = vm_path
        self.timeout = timeout

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        output_path = test_case.case_dir / ".eclipse_refactoring_result.json"
        if output_path.exists():
            output_path.unlink()
        workspace = (self.data_root / test_case.test_id).resolve()
        data_root = self.data_root.resolve()
        if data_root not in workspace.parents and workspace != data_root:
            raise ValueError(f"Eclipse workspace escaped data root: {workspace}")
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        operation = test_case.metadata.get("operation", {}) or {}
        refactoring = normalize_refactoring_type(str(operation.get("type") or test_case.metadata.get("refactoring") or ""))
        target_method = str(operation.get("target_method") or operation.get("target_symbol") or "")
        is_rename_refactoring = refactoring.lower().startswith("rename")
        target_kind = rename_target_kind(refactoring, operation) if is_rename_refactoring else str(operation.get("target_kind") or "")
        target_symbol = str(operation.get("target_symbol") or "")
        if is_rename_refactoring:
            rename_target = rename_target_symbol(operation)
            if rename_target:
                target_symbol = rename_target
        target_name = str(operation.get("target_name") or "")
        target_hint = str(operation.get("target_location_hint") or "")
        new_name = str(operation.get("new_name") or operation.get("target_name") or "")
        operation_options = _operation_options(test_case, operation, refactoring, preserve_newlines=True)
        if refactoring in {"ExtractVariable", "ExtractMethod"} and operation_options:
            new_name = str(operation_options["new_name"])
        java_file = test_case.java_files[0] if test_case.java_files else Path("")
        command = [
            str(self.eclipse_home / "eclipsec.exe"),
        ]
        if self.vm_path:
            command.extend(["-vm", self.vm_path])
        command.extend([
            "-nosplash",
            "-consoleLog",
            "-configuration",
            str(self.configuration),
            "-data",
            str(workspace),
            "-application",
            self.application_id,
            "--case-dir",
            str(test_case.case_dir),
            "--java-file",
            str(java_file),
            "--refactoring",
            refactoring,
            "--target-method",
            target_method,
        ])
        if target_symbol:
            command.extend(["--target-symbol", target_symbol])
        if target_kind:
            command.extend(["--target-kind", target_kind])
        if target_name:
            command.extend(["--target-name", target_name])
        if target_hint:
            command.extend(["--target-hint", target_hint])
        if new_name:
            command.extend(["--new-name", new_name])
        if operation_options:
            command.extend(["--target-start", str(operation_options["target_start"])])
            command.extend(["--target-length", str(operation_options["target_length"])])
            if refactoring == "ExtractVariable":
                command.extend(["--replace-all", str(operation_options["replace_all"]).lower()])
            elif refactoring == "ExtractMethod":
                command.extend(["--replace-duplicates", str(operation_options["replace_duplicates"]).lower()])
        command.extend(["--output", str(output_path)])
        runner = run_command(command, cwd=test_case.case_dir, timeout=self.timeout)

        if output_path.exists():
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raw = {
                    "status": "failed",
                    "applied": False,
                    "severity": "ERROR",
                    "messages": [f"Eclipse backend wrote invalid JSON: {exc}"],
                }
        else:
            raw = {
                "status": "failed" if runner["status"] != "timeout" else "timeout",
                "applied": False,
                "severity": "ERROR",
                "messages": ["Eclipse backend did not write a refactoring result JSON file."],
            }
        result = normalize_refactoring_result(raw, backend=self.name)
        result["runner"] = runner
        if result["status"] not in {"failed", "timeout"}:
            shutil.rmtree(workspace, ignore_errors=True)
        return result


class EclipseRefactoringRunner:
    def __init__(
        self,
        backend: Any | None = None,
        java_path: str = "java",
        javac_path: str = "javac",
        compile_timeout: int = 10,
        run_timeout: int = 5,
    ) -> None:
        self.backend = backend or UnsupportedEclipseBackend()
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
                "messages": ["Before-refactoring compilation failed; Eclipse refactoring was skipped."],
                "changed_files": [],
                "diff": "",
                "runner": {},
            }
            after: Dict[str, Any] = not_run_after("before_compile_failed")
            behavior_preserved = None
        else:
            snapshot_before = snapshot_java_files(workspace_case.case_dir)
            refactoring = normalize_refactoring_result(
                self.backend.apply(workspace_case),
                backend=backend_name(self.backend),
            )
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
            "schema_version": ECLIPSE_SCHEMA_VERSION,
            "engine": "Eclipse",
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


def copy_case_to_workspace(test_case: TestCase, workspace_root: str | Path) -> TestCase:
    root = Path(workspace_root).resolve()
    target = (root / test_case.test_id).resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"workspace target escaped workspace root: {target}")
    for attempt in range(5):
        if not target.exists():
            break
        try:
            shutil.rmtree(target)
            break
        except PermissionError as exc:
            if attempt == 4:
                raise PermissionError(
                    f"Workspace copy is still in use: {target}. "
                    "Retry with a fresh --workspace-root."
                ) from exc
            time.sleep(0.25 * (attempt + 1))
    shutil.copytree(test_case.case_dir, target)

    java_files = []
    for path in test_case.java_files:
        rel = path.resolve().relative_to(test_case.case_dir.resolve())
        java_files.append((target / rel).resolve())

    source_index = test_case.source_index
    if str(source_index):
        try:
            source_index = (target / source_index.resolve().relative_to(test_case.case_dir.resolve())).resolve()
        except ValueError:
            source_index = test_case.source_index

    return TestCase(
        test_id=test_case.test_id,
        case_dir=target,
        java_files=java_files,
        main_class=test_case.main_class,
        source_index=source_index,
        metadata=test_case.metadata,
    )


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


def mark_no_effect_success_as_failed(refactoring: Dict[str, Any], changed_files: List[str], diff: str) -> None:
    """Do not count a backend-reported success if the workspace source did not change."""
    status = str(refactoring.get("status") or "").strip().lower()
    if not refactoring.get("applied") or status not in {"success", "warning"}:
        return
    if changed_files or diff:
        return
    refactoring["status"] = "failed"
    refactoring["applied"] = False
    refactoring["severity"] = "ERROR"
    refactoring["no_source_change_detected"] = True
    messages = refactoring.get("messages")
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        messages = []
    messages.append(
        "Backend reported the refactoring as applied, but the runner detected no Java source changes."
    )
    refactoring["messages"] = messages


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


def compare_behavior(before: Dict[str, Any], after: Dict[str, Any]) -> bool | None:
    before_run = before.get("run", {}) or {}
    after_run = after.get("run", {}) or {}
    if before_run.get("status") != "ok" or after_run.get("status") != "ok":
        return None
    return (
        before_run.get("returncode") == after_run.get("returncode")
        and str(before_run.get("stdout", "")) == str(after_run.get("stdout", ""))
        and str(before_run.get("stderr", "")) == str(after_run.get("stderr", ""))
    )


def snapshot_java_files(case_dir: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in sorted(case_dir.rglob("*.java")):
        rel = path.relative_to(case_dir).as_posix()
        snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def diff_snapshots(before: Dict[str, str], after: Dict[str, str]) -> tuple[List[str], str]:
    changed: List[str] = []
    chunks: List[str] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel, "")
        new = after.get(rel, "")
        if old == new:
            continue
        changed.append(rel)
        chunks.extend(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=f"before/{rel}",
                tofile=f"after/{rel}",
                lineterm="",
            )
        )
    return changed, "\n".join(chunks)


def backend_name(backend: Any) -> str:
    return str(getattr(backend, "name", backend.__class__.__name__))
