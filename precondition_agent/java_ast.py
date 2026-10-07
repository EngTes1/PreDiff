import json
import os
import shutil
import subprocess
import time

from pathlib import Path
from typing import Any, Dict, Optional


class JavaAstMixin:
    def _helper_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "tools" / "java_ast_helper"

    def _helper_build_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / ".ast_helper_build"

    def _resolve_java_tool(self, configured: str, fallback_name: str) -> str:
        if configured and os.path.isfile(configured):
            return configured
        tool = configured or fallback_name
        resolved = shutil.which(tool)
        return resolved or tool

    def _ensure_ast_helper_ready(self) -> bool:
        started_at = time.time()
        if not self.config.enable_ast_retrieval:
            self._trace(
                "ast_helper",
                {"status": "disabled_by_config"},
                started_at,
            )
            return False
        if self._ast_disabled_reason:
            self._trace(
                "ast_helper",
                {"status": "disabled", "reason": self._ast_disabled_reason},
                started_at,
            )
            return False
        if self._ast_helper_ready is True:
            self._trace(
                "ast_helper",
                {"status": "ready_cached"},
                started_at,
            )
            return True

        helper_source = self._helper_root() / "JavaAstParserCli.java"
        if not helper_source.exists():
            self._ast_disabled_reason = f"helper_source_missing:{helper_source}"
            self._trace(
                "ast_helper",
                {"status": "missing_source", "path": str(helper_source)},
                started_at,
            )
            return False

        build_dir = self._helper_build_dir()
        build_dir.mkdir(parents=True, exist_ok=True)
        class_file = build_dir / "JavaAstParserCli.class"
        javac = self._resolve_java_tool(self.config.javac_path, "javac")

        need_compile = (
            not class_file.exists()
            or class_file.stat().st_mtime < helper_source.stat().st_mtime
        )
        if not need_compile:
            self._ast_helper_ready = True
            self._trace(
                "ast_helper",
                {"status": "ready", "compiled": False, "build_dir": str(build_dir)},
                started_at,
            )
            return True

        process = subprocess.run(
            [javac, "-d", str(build_dir), str(helper_source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if process.returncode != 0:
            self._ast_disabled_reason = (process.stderr or "javac failed").strip()
            self._trace(
                "ast_helper",
                {
                    "status": "compile_failed",
                    "javac": javac,
                    "error": self._ast_disabled_reason,
                },
                started_at,
            )
            return False

        self._ast_helper_ready = True
        self._trace(
            "ast_helper",
            {"status": "ready", "compiled": True, "build_dir": str(build_dir)},
            started_at,
        )
        return True

    def _parse_java_file_with_ast(self, path: str) -> Optional[Dict[str, Any]]:
        started_at = time.time()
        if path in self._ast_cache:
            cached = self._ast_cache[path]
            self._trace(
                "ast_parse_file",
                {
                    "status": "cached",
                    "language": "java",
                    "path": path,
                    "types": len((cached or {}).get("types", [])),
                    "methods": len((cached or {}).get("methods", [])),
                },
                started_at,
            )
            return cached

        if not self._ensure_ast_helper_ready():
            self._trace(
                "ast_parse_file",
                {
                    "status": "helper_unavailable",
                    "language": "java",
                    "path": path,
                    "reason": self._ast_disabled_reason or "helper_not_ready",
                },
                started_at,
            )
            self._ast_cache[path] = None
            return None

        java = self._resolve_java_tool(self.config.java_path, "java")
        build_dir = self._helper_build_dir()
        process = subprocess.run(
            [java, "-cp", str(build_dir), "JavaAstParserCli", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if process.returncode != 0:
            self._trace(
                "ast_parse_file",
                {
                    "status": "parse_failed",
                    "language": "java",
                    "path": path,
                    "java": java,
                    "error": (process.stderr or "java helper failed").strip(),
                },
                started_at,
            )
            self._ast_cache[path] = None
            return None

        try:
            parsed = json.loads(process.stdout)
        except Exception as exc:
            self._trace(
                "ast_parse_file",
                {
                    "status": "invalid_json",
                    "language": "java",
                    "path": path,
                    "error": str(exc),
                },
                started_at,
            )
            self._ast_cache[path] = None
            return None

        parsed["language"] = "java"
        self._ast_cache[path] = parsed
        self._trace(
            "ast_parse_file",
            {
                "status": "ok",
                "language": "java",
                "path": path,
                "types": len(parsed.get("types", [])),
                "methods": len(parsed.get("methods", [])),
            },
            started_at,
        )
        return parsed

