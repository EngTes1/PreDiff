import os
import shutil
import subprocess
import time

from pathlib import Path
from typing import Any, Dict, List, Optional

from refactoring_test_runner.discovery import TestCase


def compile_and_run_case(
    test_case: TestCase,
    java_path: str = "java",
    javac_path: str = "javac",
    compile_timeout: int = 10,
    run_timeout: int = 5,
    clean: bool = True,
) -> Dict[str, Any]:
    started_at = time.time()
    out_dir = test_case.case_dir / "out"
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    compile_result = compile_case(
        test_case,
        out_dir=out_dir,
        javac_path=javac_path,
        timeout=compile_timeout,
    )
    if compile_result["status"] != "ok":
        run_result = not_run_result("compile_failed")
    else:
        run_result = run_case(
            test_case,
            out_dir=out_dir,
            java_path=java_path,
            timeout=run_timeout,
        )

    return {
        "test_id": test_case.test_id,
        "case_dir": str(test_case.case_dir),
        "java_files": [str(path) for path in test_case.java_files],
        "main_class": test_case.main_class,
        "source_index": str(test_case.source_index) if str(test_case.source_index) else "",
        "compile": compile_result,
        "run": run_result,
        "duration_ms": int((time.time() - started_at) * 1000),
    }


def compile_case(
    test_case: TestCase,
    out_dir: Path,
    javac_path: str = "javac",
    timeout: int = 10,
) -> Dict[str, Any]:
    command = [
        javac_path,
        "-encoding",
        "UTF-8",
        "-d",
        str(out_dir),
    ] + [str(path) for path in test_case.java_files]
    return run_command(command, cwd=test_case.case_dir, timeout=timeout)


def run_case(
    test_case: TestCase,
    out_dir: Path,
    java_path: str = "java",
    timeout: int = 5,
) -> Dict[str, Any]:
    command = [java_path, "-cp", str(out_dir), test_case.main_class]
    return run_command(command, cwd=test_case.case_dir, timeout=timeout)


def run_command(command: List[str], cwd: Path, timeout: int, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    started_at = time.time()
    process_env = None
    if env is not None:
        process_env = os.environ.copy()
        process_env.update({str(key): str(value) for key, value in env.items()})
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "status": "ok" if process.returncode == 0 else "failed",
            "command": command,
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.time() - started_at) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                process.kill()
        except Exception:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return {
            "status": "timeout",
            "command": command,
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.time() - started_at) * 1000),
        }


def not_run_result(reason: str) -> Dict[str, Any]:
    return {
        "status": "not_run",
        "reason": reason,
        "command": [],
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "duration_ms": 0,
    }
