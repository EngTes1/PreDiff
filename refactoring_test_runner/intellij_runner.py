import json
import os
import re
import shutil
import subprocess
import time
from html import escape
from pathlib import Path
from typing import Any, Dict

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


INTELLIJ_SCHEMA_VERSION = "intellij_refactoring_run_result_v1"


class IntelliJApplicationBackend:
    name = "intellij_application"

    def __init__(
        self,
        intellij_home: str | Path,
        plugin_root: str | Path,
        data_root: str | Path,
        timeout: int = 240,
    ) -> None:
        self.intellij_home = normalize_intellij_home(Path(intellij_home).resolve())
        self.plugin_root = Path(plugin_root).resolve()
        self.data_root = Path(data_root).resolve()
        self.timeout = timeout

    def apply(self, test_case: TestCase) -> Dict[str, Any]:
        output_path = test_case.case_dir / ".intellij_refactoring_result.json"
        if output_path.exists():
            output_path.unlink()
        config_dir = (self.data_root / "config" / test_case.test_id).resolve()
        system_dir = (self.data_root / "system" / test_case.test_id).resolve()
        properties_file = (self.data_root / "properties" / f"{test_case.test_id}.properties").resolve()
        vmoptions_file = (self.data_root / "vmoptions" / f"{test_case.test_id}.vmoptions").resolve()
        clean_intellij_case_state(self.data_root, config_dir, system_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        system_dir.mkdir(parents=True, exist_ok=True)
        properties_file.parent.mkdir(parents=True, exist_ok=True)
        vmoptions_file.parent.mkdir(parents=True, exist_ok=True)
        write_trusted_paths(config_dir, test_case.case_dir)
        write_startup_options(config_dir)
        seed_intellij_user_preferences()
        properties_file.write_text(
            "\n".join(
                [
                    f"idea.config.path={to_idea_path(config_dir)}",
                    f"idea.system.path={to_idea_path(system_dir)}",
                    f"idea.plugins.path={to_idea_path(self.plugin_root)}",
                    "idea.is.internal=false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        vmoptions_file.write_text(
            "\n".join(
                [
                    "-Xms64m",
                    "-Xmx1024m",
                    "-XX:ReservedCodeCacheSize=256m",
                    "-XX:+IgnoreUnrecognizedVMOptions",
                    "-Dfile.encoding=UTF-8",
                    "-Djava.awt.headless=true",
                    "-Duser.language=en",
                    "-Duser.country=US",
                    "-Duser.region=US",
                    "-Dsun.io.useCanonCaches=false",
                    "-Dide.show.tips.on.startup.default.value=false",
                    "-Djb.consents.confirmation.enabled=false",
                    "-Dintellij.first.ide.session=false",
                    "-Djava.nio.file.spi.DefaultFileSystemProvider=com.intellij.platform.core.nio.fs.MultiRoutingFileSystemProvider",
                    "--add-opens=java.base/jdk.internal.org.objectweb.asm.tree=ALL-UNNAMED",
                    "--add-opens=java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED",
                    "",
                ]
            ),
            encoding="utf-8",
        )

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
            operation_options = extract_method_options(test_case, operation)
        elif refactoring == "InlineMethod":
            operation_options = inline_method_options(test_case, operation, preserve_newlines=False)
        else:
            operation_options = {}
        if refactoring in {"ExtractVariable", "ExtractMethod"} and operation_options:
            new_name = str(operation_options["new_name"])
        java_file = test_case.java_files[0] if test_case.java_files else Path("")
        launcher_home = self.launcher_home()
        command = [
            str(launcher_home / "bin" / "idea.bat"),
            "ij-refactor-test",
            f"--refactoring={refactoring}",
            f"--case-dir={test_case.case_dir}",
            f"--java-file={java_file}",
            f"--target-method={target_method}",
            f"--target-symbol={target_symbol}",
            f"--target-kind={target_kind}",
            f"--target-hint={target_hint}",
            f"--new-name={new_name}",
            f"--target-start={operation_options.get('target_start', -1)}",
            f"--target-length={operation_options.get('target_length', 0)}",
            f"--replace-all={str(operation_options.get('replace_all', False)).lower()}",
            f"--replace-duplicates={str(operation_options.get('replace_duplicates', False)).lower()}",
            f"--output={output_path}",
        ]
        runner = run_command(
            command,
            cwd=test_case.case_dir,
            timeout=self.timeout,
            env=intellij_process_env(launcher_home, properties_file, vmoptions_file),
        )

        if output_path.exists():
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raw = {
                    "status": "failed",
                    "applied": False,
                    "severity": "ERROR",
                    "messages": [f"IntelliJ backend wrote invalid JSON: {exc}"],
                }
        else:
            raw = {
                "status": "failed" if runner["status"] != "timeout" else "timeout",
                "applied": False,
                "severity": "ERROR",
                "messages": ["IntelliJ backend did not write a refactoring result JSON file."],
            }
        result = normalize_refactoring_result(raw, backend=self.name)
        result["runner"] = runner
        if result["status"] not in {"failed", "timeout"}:
            shutil.rmtree(config_dir, ignore_errors=True)
            shutil.rmtree(system_dir, ignore_errors=True)
            properties_file.unlink(missing_ok=True)
            vmoptions_file.unlink(missing_ok=True)
        return result

    def launcher_home(self) -> Path:
        link = self.data_root / "ascii_home" / intellij_launcher_link_name(self.intellij_home)
        if self.intellij_home == link:
            return self.intellij_home
        ensure_directory_junction(link, self.intellij_home)
        return link


class IntelliJRefactoringRunner:
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
                "messages": ["Before-refactoring compilation failed; IntelliJ refactoring was skipped."],
                "changed_files": [],
                "diff": "",
                "runner": {},
            }
            after = not_run_after("before_compile_failed")
            behavior_preserved = None
        else:
            snapshot_before = snapshot_java_files(workspace_case.case_dir)
            prepare_intellij_project(workspace_case)
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
            "schema_version": INTELLIJ_SCHEMA_VERSION,
            "engine": "IntelliJ IDEA",
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


def normalize_intellij_home(path: Path) -> Path:
    if path.name.lower() == "bin" and (path.parent / "product-info.json").exists():
        return path.parent
    return path


def intellij_launcher_link_name(intellij_home: Path) -> str:
    product_info_path = intellij_home / "product-info.json"
    try:
        product_info = json.loads(product_info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        product_info = {}
    product_code = str(product_info.get("productCode") or "IDEA")
    build_number = str(product_info.get("buildNumber") or product_info.get("version") or "unknown")
    identity = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{product_code}_{build_number}").strip("_")
    return f"IntelliJIdea_{identity or 'unknown'}"


def is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def ensure_directory_junction(link: Path, target: Path) -> None:
    if link.exists():
        try:
            if link.samefile(target):
                return
        except OSError:
            pass
        raise RuntimeError(f"IntelliJ ASCII launcher path already exists but points elsewhere: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        raise RuntimeError(
            "Failed to create IntelliJ ASCII directory junction. "
            f"link={link}; target={target}; stdout={process.stdout.strip()}; stderr={process.stderr.strip()}"
        )


def clean_intellij_case_state(data_root: Path, *paths: Path) -> None:
    root = data_root.resolve()
    for path in paths:
        target = path.resolve()
        if target == root or root not in target.parents:
            raise ValueError(f"IntelliJ cache cleanup target escaped data root: {target}")
        if target.exists():
            shutil.rmtree(target)


def intellij_process_env(launcher_home: Path, properties_file: Path, vmoptions_file: Path) -> Dict[str, str]:
    jbr_bin = launcher_home / "jbr" / "bin"
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    path_entries = [
        jbr_bin,
        system_root / "System32",
        system_root,
        system_root / "System32" / "Wbem",
        system_root / "System32" / "WindowsPowerShell" / "v1.0",
    ]
    clean_path = ";".join(str(path) for path in path_entries)
    return {
        "IDEA_PROPERTIES": str(properties_file),
        "IDEA_VM_OPTIONS": str(vmoptions_file),
        "IDEA_JDK": str(launcher_home / "jbr"),
        "PATH": clean_path,
        "Path": clean_path,
    }


def prepare_intellij_project(test_case: TestCase) -> None:
    case_dir = test_case.case_dir
    idea_dir = case_dir / ".idea"
    idea_dir.mkdir(parents=True, exist_ok=True)
    module_name = test_case.test_id
    module_file = f"{module_name}.iml"

    modules_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/{escape(module_file)}" filepath="$PROJECT_DIR$/{escape(module_file)}" />
    </modules>
  </component>
</project>
"""
    (idea_dir / "modules.xml").write_text(modules_xml, encoding="utf-8")
    misc_xml = """<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" languageLevel="JDK_21" default="true" project-jdk-name="21" project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/out" />
  </component>
</project>
"""
    (idea_dir / "misc.xml").write_text(misc_xml, encoding="utf-8")
    iml = """<?xml version="1.0" encoding="UTF-8"?>
<module type="JAVA_MODULE" version="4">
  <component name="NewModuleRootManager" LANGUAGE_LEVEL="JDK_21" inherit-compiler-output="true">
    <exclude-output />
    <content url="file://$MODULE_DIR$">
      <sourceFolder url="file://$MODULE_DIR$/src" isTestSource="false" />
      <excludeFolder url="file://$MODULE_DIR$/out" />
    </content>
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>
"""
    (case_dir / module_file).write_text(iml, encoding="utf-8")


def to_idea_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def write_trusted_paths(config_dir: Path, case_dir: Path) -> None:
    options_dir = config_dir / "options"
    options_dir.mkdir(parents=True, exist_ok=True)
    trusted_paths = [
        str(case_dir.resolve()),
        str(case_dir.resolve().parent),
    ]
    entries = "\n".join(
        f'        <entry key="{escape(path)}" value="true" />' for path in trusted_paths
    )
    trusted_xml = f"""<application>
  <component name="Trusted.Paths">
    <option name="TRUSTED_PROJECT_PATHS">
      <map>
{entries}
      </map>
    </option>
  </component>
  <component name="Trusted.Paths.Settings">
    <option name="TRUSTED_PATHS">
      <list>
{chr(10).join(f'        <option value="{escape(path)}" />' for path in trusted_paths)}
      </list>
    </option>
  </component>
</application>
"""
    (options_dir / "trusted-paths.xml").write_text(trusted_xml, encoding="utf-8")


def write_startup_options(config_dir: Path) -> None:
    options_dir = config_dir / "options"
    options_dir.mkdir(parents=True, exist_ok=True)

    other_xml = """<application>
  <component name="LangManager">
    <option name="languageName" value="JAVA" />
  </component>
  <component name="PropertyService"><![CDATA[{
  "keyToString": {
    "RunOnceActivity.git.modal.commit.toggle": "true",
    "experimental.ui.on.first.startup": "true",
    "ide.show.tips.on.startup": "false",
    "trial.state.last.availability.state": "false",
    "trial.state.last.state": "FREE"
  }
}]]></component>
</application>
"""
    (options_dir / "other.xml").write_text(other_xml, encoding="utf-8")

    ide_general_xml = """<application>
  <component name="GeneralSettings">
    <option name="showTipsOnStartup" value="false" />
  </component>
  <component name="Registry">
    <entry key="ide.experimental.ui" value="true" source="SYSTEM" />
  </component>
</application>
"""
    (options_dir / "ide.general.xml").write_text(ide_general_xml, encoding="utf-8")

    ai_promo_xml = """<application>
  <component name="AIOnboardingPromoWindowAdvisor">
    <option name="attempts" value="99" />
  </component>
</application>
"""
    (options_dir / "AIOnboardingPromoWindowAdvisor.xml").write_text(ai_promo_xml, encoding="utf-8")

    usage_xml = """<application>
  <component name="UsageStatisticsPersistenceComponent">
    <option name="allowed" value="false" />
  </component>
</application>
"""
    (options_dir / "usage.statistics.xml").write_text(usage_xml, encoding="utf-8")


def seed_intellij_user_preferences() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    values = [
        (r"Software\JavaSoft\Prefs\jetbrains\privacy_policy", "accepted_version", "999.999"),
        (r"Software\JavaSoft\Prefs\jetbrains\region", "code", "not_set"),
    ]
    for subkey, name, value in values:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        except OSError:
            continue
