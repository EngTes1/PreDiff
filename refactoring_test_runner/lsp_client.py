import json
import os
import subprocess
import threading
import time

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class LspClient:
    def __init__(
        self,
        command: List[str],
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        apply_edit_handler: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.process: Optional[subprocess.Popen[str]] = None
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._running = False
        self._reader: Optional[threading.Thread] = None
        self.notifications: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.apply_edit_handler = apply_edit_handler

    def start(self) -> None:
        if self.process is not None:
            return
        process_env = os.environ.copy()
        if self.env:
            process_env.update({str(k): str(v) for k, v in self.env.items()})
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def stop(self) -> None:
        self._running = False
        process = self.process
        if process is None:
            return
        try:
            self.notify("exit", {})
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                        capture_output=True,
                        timeout=10,
                    )
                else:
                    process.kill()
            except Exception:
                pass

    def request(self, method: str, params: Any, timeout: int = 30) -> Any:
        request_id = self._next_id
        self._next_id += 1
        event = threading.Event()
        with self._pending_lock:
            self._pending[request_id] = {"event": event, "response": None}
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"LSP request timed out: {method}")
        with self._pending_lock:
            response = self._pending.pop(request_id, {}).get("response")
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
        return response.get("result") if isinstance(response, dict) else None

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("LSP process is not running")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + data)
        self.process.stdin.flush()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while self._running:
            message = self._read_message()
            if message is None:
                break
            if "id" in message and ("result" in message or "error" in message):
                with self._pending_lock:
                    pending = self._pending.get(message["id"])
                    if pending:
                        pending["response"] = message
                        pending["event"].set()
                continue
            if "method" in message and "id" in message:
                self.requests.append(message)
                self._handle_server_request(message)
                continue
            if "method" in message:
                self.notifications.append(message)

    def _read_message(self) -> Optional[Dict[str, Any]]:
        assert self.process is not None and self.process.stdout is not None
        headers: Dict[str, str] = {}
        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            line_text = line.decode("ascii", errors="replace").strip()
            if line_text == "":
                break
            if ":" in line_text:
                key, value = line_text.split(":", 1)
                headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = self.process.stdout.read(length)
        return json.loads(body.decode("utf-8", errors="replace"))

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if method == "workspace/configuration":
            items = ((message.get("params") or {}).get("items") or [])
            result = [default_configuration(item) for item in items]
        elif method == "workspace/applyEdit":
            edit = (message.get("params") or {}).get("edit") or {}
            applied = False
            failure = ""
            if self.apply_edit_handler:
                try:
                    applied = bool(self.apply_edit_handler(edit))
                except Exception as exc:
                    failure = str(exc)
            else:
                failure = "No workspace/applyEdit handler is configured."
            result = {"applied": applied}
            if failure:
                result["failureReason"] = failure
        elif method in {"client/registerCapability", "client/unregisterCapability"}:
            result = None
        elif method == "window/showMessageRequest":
            result = None
        else:
            result = None
        self._send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def stderr_text(self) -> str:
        process = self.process
        if process is None or process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            return ""


def default_configuration(item: Dict[str, Any]) -> Any:
    section = str(item.get("section") or "")
    if section == "java.home":
        return os.environ.get("JAVA_HOME")
    if section == "java.project.sourcePaths":
        return ["src"]
    if section == "java.project.outputPath":
        return "bin"
    if section == "java.import.gradle.enabled":
        return False
    if section == "java.import.maven.enabled":
        return False
    if section == "java.autobuild.enabled":
        return False
    if section == "java.errors.incompleteClasspath.severity":
        return "ignore"
    if section == "java.configuration.updateBuildConfiguration":
        return "disabled"
    if section == "java.format.enabled":
        return False
    return None


def wait_for(predicate, timeout: int = 30, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
