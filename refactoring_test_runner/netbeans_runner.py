import json
import os
import re
import shutil
import time
from html import escape

from pathlib import Path
from typing import Any, Dict, List

from refactoring_test_runner.compile_run import compile_and_run_case, not_run_result, run_command
from refactoring_test_runner.discovery import TestCase
from refactoring_test_runner.extract_method import extract_method_options
from refactoring_test_runner.extract_variable import extract_variable_options
from refactoring_test_runner.inline_method import inline_method_options
from refactoring_test_runner.eclipse_runner import (
    compare_behavior,
    copy_case_to_workspace,
    diff_snapshots,
    mark_no_effect_success_as_failed,
    normalize_refactoring_type,
    rename_target_kind,
    rename_target_symbol,
    snapshot_java_files,
)


NETBEANS_SCHEMA_VERSION = "netbeans_refactoring_run_result_v1"
MINIMUM_BACKEND_JDK_VERSION = 21


class NetBeansApplicationBackend:
    name = "netbeans_application"

    def __init__(
        self,
        netbeans_home: str | Path,
        backend_cluster: str | Path,
        data_root: str | Path,
        jdk_home: str | Path = "",
        timeout: int = 180,
    ) -> None:
        self.netbeans_home = normalize_netbeans_home(Path(netbeans_home).resolve())
        self.backend_cluster = Path(backend_cluster).resolve()
        self.data_root = Path(data_root).resolve()
        self.jdk_home = validate_jdk_home(Path(jdk_home))
        self.timeout = timeout

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        output_path = test_case.case_dir / ".netbeans_refactoring_result.json"
        if output_path.exists():
            output_path.unlink()
        userdir = (self.data_root / "userdir" / test_case.test_id).resolve()
        cachedir = (self.data_root / "cachedir" / test_case.test_id).resolve()
        for state_dir in (userdir, cachedir):
            if state_dir.exists():
                shutil.rmtree(state_dir)
        userdir.mkdir(parents=True, exist_ok=True)
        cachedir.mkdir(parents=True, exist_ok=True)

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
        target_hint = str(operation.get("target_location_hint") or "")
        new_name = str(operation.get("new_name") or operation.get("target_name") or "")
        if refactoring == "ExtractVariable":
            operation_options = extract_variable_options(test_case, operation)
        elif refactoring == "ExtractMethod":
            operation_options = extract_method_options(test_case, operation, preserve_newlines=True)
        elif refactoring == "InlineMethod":
            operation_options = inline_method_options(test_case, operation, preserve_newlines=False)
        else:
            operation_options = {}
        if refactoring in {"ExtractVariable", "ExtractMethod"} and operation_options:
            new_name = str(operation_options["new_name"])
        java_file = test_case.java_files[0] if test_case.java_files else Path("")
        command = [
            str(self.netbeans_home / "platform" / "lib" / "nbexec64.exe"),
            "--userdir",
            str(userdir),
            "--cachedir",
            str(cachedir),
            "--branding",
            "nb",
            "--clusters",
            self.clusters_arg(),
        ]
        command.extend(["--jdkhome", str(self.jdk_home)])
        command.extend([
            "-J-Dnetbeans.logger.console=true",
            "-J-Dnetbeans.close=true",
            "-J-Dnetbeans.full.hack=true",
            "-J-Dplugin.manager.check.updates=false",
            "-J-Dnetbeans.autoupdate.enabled=false",
            "-J--add-opens=java.base/java.net=ALL-UNNAMED",
            "-J--add-opens=java.base/java.lang=ALL-UNNAMED",
            "-J--add-opens=java.base/java.lang.ref=ALL-UNNAMED",
            "-J--add-opens=java.base/java.util=ALL-UNNAMED",
            "-J--add-opens=java.base/java.nio=ALL-UNNAMED",
            "-J--add-opens=java.base/java.security=ALL-UNNAMED",
            "-J--add-opens=java.prefs/java.util.prefs=ALL-UNNAMED",
            "-J--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
            "-J--add-opens=java.desktop/javax.swing.text=ALL-UNNAMED",
            "-J--add-opens=java.desktop/javax.swing.plaf.basic=ALL-UNNAMED",
            "-J--add-opens=java.desktop/javax.swing.plaf.synth=ALL-UNNAMED",
            "-J--add-opens=java.desktop/java.awt=ALL-UNNAMED",
            "-J--add-opens=java.desktop/java.awt.event=ALL-UNNAMED",
            "-J--add-opens=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
            "-J--add-opens=jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
            "-J--add-opens=jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED",
            "-J--add-opens=jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED",
            "-J--add-opens=jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED",
            f"-J-Dnb.refactor.type={refactoring}",
            f"-J-Dnb.refactor.caseDir={test_case.case_dir}",
            f"-J-Dnb.refactor.javaFile={java_file}",
            f"-J-Dnb.refactor.targetMethod={target_method}",
            f"-J-Dnb.refactor.targetSymbol={target_symbol}",
            f"-J-Dnb.refactor.targetKind={target_kind}",
            f"-J-Dnb.refactor.targetHint={target_hint}",
            f"-J-Dnb.refactor.newName={new_name}",
            f"-J-Dnb.refactor.targetStart={operation_options.get('target_start', -1)}",
            f"-J-Dnb.refactor.targetLength={operation_options.get('target_length', 0)}",
            f"-J-Dnb.refactor.replaceAll={str(operation_options.get('replace_all', False)).lower()}",
            f"-J-Dnb.refactor.replaceDuplicates={str(operation_options.get('replace_duplicates', False)).lower()}",
            f"-J-Dnb.refactor.output={output_path}",
            "--nosplash",
        ])
        runner = run_command(command, cwd=test_case.case_dir, timeout=self.timeout)

        if output_path.exists():
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raw = {
                    "status": "failed",
                    "applied": False,
                    "severity": "ERROR",
                    "messages": [f"NetBeans backend wrote invalid JSON: {exc}"],
                }
        else:
            raw = {
                "status": "failed" if runner["status"] != "timeout" else "timeout",
                "applied": False,
                "severity": "ERROR",
                "messages": ["NetBeans backend did not write a refactoring result JSON file."],
            }
        result = normalize_refactoring_result(raw, backend=self.name)
        result["runner"] = runner
        if result["status"] not in {"failed", "timeout"}:
            shutil.rmtree(userdir, ignore_errors=True)
            shutil.rmtree(cachedir, ignore_errors=True)
        return result

    def clusters_arg(self) -> str:
        clusters: List[str] = []
        cluster_names = self.installed_cluster_names()
        for item in cluster_names:
            if Path(item).name.lower() not in {"platform", "nb", "ide", "extide", "java"}:
                continue
            path = Path(item)
            if not path.is_absolute():
                path = self.netbeans_home / item
            if path.exists():
                clusters.append(str(path.resolve()))
        clusters.append(str(self.backend_cluster))
        return ";".join(clusters)

    def installed_cluster_names(self) -> List[str]:
        clusters_file = self.netbeans_home / "etc" / "netbeans.clusters"
        if not clusters_file.exists():
            return ["platform", "nb", "ergonomics", "ide", "extide", "java", "apisupport", "harness"]
        result: List[str] = []
        seen = set()
        for raw_line in clusters_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key = line.lower()
            path = Path(line)
            if not path.is_absolute():
                path = self.netbeans_home / line
            if key in seen or not path.exists():
                continue
            seen.add(key)
            result.append(line)
        return result


class NetBeansRefactoringRunner:
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
                "messages": ["Before-refactoring compilation failed; NetBeans refactoring was skipped."],
                "changed_files": [],
                "diff": "",
                "runner": {},
            }
            after = not_run_after("before_compile_failed")
            behavior_preserved = None
        else:
            snapshot_before = snapshot_java_files(workspace_case.case_dir)
            prepare_netbeans_project(workspace_case)
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
            "schema_version": NETBEANS_SCHEMA_VERSION,
            "engine": "NetBeans",
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


def normalize_netbeans_home(path: Path) -> Path:
    if path.name.lower() == "bin" and (path.parent / "etc" / "netbeans.clusters").exists():
        return path.parent
    return path


def resolve_netbeans_jdk_home(jdk_home: str | Path = "", java_path: str | Path = "java") -> Path:
    candidates: List[Path] = []
    if str(jdk_home).strip():
        candidates.append(Path(jdk_home))

    java_executable = shutil.which(str(java_path))
    if java_executable:
        candidates.append(Path(java_executable).parent.parent)
    else:
        java_candidate = Path(java_path)
        if java_candidate.is_file():
            candidates.append(java_candidate.parent.parent)

    java_home = os.getenv("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home))

    failures: List[str] = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_jdk_home(candidate)
        key = str(normalized).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return validate_jdk_home(normalized)
        except (FileNotFoundError, ValueError) as exc:
            failures.append(str(exc))

    detail = "; ".join(failures) if failures else "no JDK candidate was provided or discovered"
    raise FileNotFoundError(
        "NetBeans runner requires a valid JDK 21+ home. "
        "Pass --jdk-home or an absolute --java-path. "
        f"Resolution details: {detail}"
    )


def normalize_jdk_home(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if candidate.is_file() and candidate.name.lower() in {"java", "java.exe"}:
        return candidate.parent.parent
    if candidate.name.lower() == "bin":
        return candidate.parent
    return candidate


def validate_jdk_home(path: Path) -> Path:
    candidate = normalize_jdk_home(path)
    java_executable = candidate / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not java_executable.is_file():
        raise FileNotFoundError(f"Java executable not found under JDK home: {candidate}")
    major = jdk_major_version(candidate)
    if major is not None and major < MINIMUM_BACKEND_JDK_VERSION:
        raise ValueError(
            f"NetBeans backend requires JDK {MINIMUM_BACKEND_JDK_VERSION}+, "
            f"but {candidate} is JDK {major}."
        )
    return candidate


def jdk_major_version(jdk_home: Path) -> int | None:
    release_file = jdk_home / "release"
    if not release_file.is_file():
        return None
    content = release_file.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'^JAVA_VERSION="(?:1\.)?(\d+)', content, re.MULTILINE)
    return int(match.group(1)) if match else None


def prepare_netbeans_project(test_case: TestCase) -> None:
    """Create a minimal Maven project around the generated src folder for NetBeans."""
    case_dir = test_case.case_dir
    src_dir = case_dir / "src"
    nbproject = case_dir / "nbproject"
    if nbproject.exists():
        shutil.rmtree(nbproject)
    project_name = escape(test_case.test_id)
    main_class = escape(str(test_case.main_class or test_case.test_id))
    pom_xml = f"""<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>generated.refactoring.tests</groupId>
  <artifactId>{project_name}</artifactId>
  <version>1.0-SNAPSHOT</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <exec.mainClass>{main_class}</exec.mainClass>
  </properties>
  <build>
    <sourceDirectory>{src_dir.name}</sourceDirectory>
  </build>
</project>
"""
    (case_dir / "pom.xml").write_text(pom_xml, encoding="utf-8")
