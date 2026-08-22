#!/usr/bin/env python3
"""Minimal live-stdio protocol smoke test for mach-lsp."""

from __future__ import annotations

import argparse
import contextlib
import signal
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

HEADER_MAX = 8 * 1024
BODY_MAX = 16 * 1024 * 1024
ANY_VERSION = object()


class ProtocolError(RuntimeError):
    """Raised when the live protocol session violates an asserted contract."""


class LspSession:
    """Drive one language-server process using LSP stdio framing."""

    def __init__(self, server: Path, cwd: Path, timeout: float,
                 env_extra: dict[str, str] | None = None) -> None:
        env = os.environ.copy()
        # tracing is stripped so an operator's own MLS_TRACE cannot change what
        # the tests exercise; a test that is ABOUT tracing asks for it back
        env.pop("MLS_TRACE", None)
        env.pop("MLS_TRACE_FILE", None)
        if env_extra:
            env.update(env_extra)
        self.timeout = timeout
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [str(server)],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self.inbox: queue.Queue[object] = queue.Queue()
        self.pending: list[dict[str, Any]] = []
        self.stderr_chunks: list[bytes] = []
        self.next_id = 1
        self.message_count = 0
        self.timings: list[tuple[str, float]] = []
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                headers: dict[bytes, bytes] = {}
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    name, separator, value = line.partition(b":")
                    if not separator:
                        raise ProtocolError(f"malformed response header: {line!r}")
                    headers[name.strip().lower()] = value.strip()
                raw_length = headers.get(b"content-length")
                if raw_length is None:
                    raise ProtocolError("response has no Content-Length header")
                length = int(raw_length)
                body = self.proc.stdout.read(length)
                if length <= 0 or len(body) != length:
                    raise ProtocolError("response body length does not match Content-Length")
                message = json.loads(body)
                if not isinstance(message, dict):
                    raise ProtocolError(f"JSON-RPC message is not an object: {message!r}")
                self.inbox.put(message)
        except BaseException as error:
            self.inbox.put(error)
        finally:
            self.inbox.put(None)

    def _stderr_loop(self) -> None:
        while True:
            chunk = self.proc.stderr.read(4096)
            if not chunk:
                return
            self.stderr_chunks.append(chunk)

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ProtocolError(f"server stdin closed; stderr: {self.stderr_text()}") from error

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request, await its id, and retain its latency."""
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        started = time.monotonic()
        self._send(message)
        response = self.wait_for(lambda item: item.get("id") == request_id, f"response to {method}")
        self.timings.append((f"{method}#{request_id}", time.monotonic() - started))
        if "error" in response:
            raise ProtocolError(f"{method} returned {response['error']!r}")
        return response

    def respond_error(self, request: dict[str, Any], code: int, message: str) -> None:
        """Reject one server-initiated request."""
        self._send({"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": code, "message": message}})

    def respond_result(self, request: dict[str, Any], result: Any = None) -> None:
        """Acknowledge one server-initiated request."""
        self._send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})

    def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
    ) -> dict[str, Any]:
        """Wait for one message while retaining unrelated notifications."""
        for index, message in enumerate(self.pending):
            if predicate(message):
                return self.pending.pop(index)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"timed out waiting for {description}; stderr: {self.stderr_text()}")
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty as error:
                raise ProtocolError(f"timed out waiting for {description}") from error
            if item is None:
                raise ProtocolError(f"server exited while waiting for {description}; stderr: {self.stderr_text()}")
            if isinstance(item, BaseException):
                raise ProtocolError(f"response reader failed: {item}") from item
            assert isinstance(item, dict)
            self.message_count += 1
            if predicate(item):
                return item
            self.pending.append(item)

    def diagnostics(self, uri: str, version: object = ANY_VERSION) -> dict[str, Any]:
        """Wait for the next diagnostics notification for a document."""
        return self.wait_for(
            lambda item: (item.get("method") == "textDocument/publishDiagnostics"
                          and isinstance(item.get("params"), dict)
                          and item["params"].get("uri") == uri
                          and (version is ANY_VERSION
                               or (item["params"].get("version") == version
                                   if version is not None
                                   else "version" not in item["params"]))),
            f"diagnostics for {uri}",
        )

    def quiet_diagnostics(self, settle: float = 0.4) -> list[dict[str, Any]]:
        """Return any diagnostics published since the last wait.

        Diagnostics belong to document state, so a feature request must not
        produce one. Anything a request republished is already in `pending`
        (the request's own reply drained the inbox past it); `settle` also
        catches a publish still in flight behind that reply.
        """
        found = [m for m in self.pending
                 if m.get("method") == "textDocument/publishDiagnostics"]
        self.pending = [m for m in self.pending
                        if m.get("method") != "textDocument/publishDiagnostics"]
        deadline = time.monotonic() + settle
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return found
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty:
                return found
            if item is None or isinstance(item, BaseException):
                return found
            self.message_count += 1
            if item.get("method") == "textDocument/publishDiagnostics":
                found.append(item)
            else:
                self.pending.append(item)

    def finish(self, send_exit: bool = True) -> tuple[int, float, int]:
        """Perform shutdown/exit and return process telemetry."""
        response = self.request("shutdown")
        require(response.get("result", object()) is None, f"invalid shutdown response: {response!r}")
        if send_exit:
            self.notify("exit")
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            self.proc.kill()
            self.proc.wait()
            raise ProtocolError("server did not exit after shutdown") from error
        self._join()
        require(code == 0, f"server exited with {code}; stderr: {self.stderr_text()}")
        return code, time.monotonic() - self.started, self.message_count

    def abort(self) -> None:
        """Stop a failed session without hiding its assertion."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._join()

    def _join(self) -> None:
        self.reader.join(timeout=1)
        self.stderr_reader.join(timeout=1)

    def stderr_text(self) -> str:
        """Return captured server stderr."""
        return b"".join(self.stderr_chunks).decode(errors="replace").strip()


def require(condition: bool, message: str) -> None:
    """Raise a readable protocol assertion failure."""
    if not condition:
        raise ProtocolError(message)


def assert_position(value: Any, label: str) -> None:
    """Check the JSON shape of an LSP Position."""
    require(isinstance(value, dict), f"{label} is not an object")
    for field in ("line", "character"):
        require(type(value.get(field)) is int and value[field] >= 0, f"{label}.{field} is invalid")


def assert_range(value: Any, label: str) -> None:
    """Check the JSON shape of an LSP Range."""
    require(isinstance(value, dict), f"{label} is not an object")
    assert_position(value.get("start"), f"{label}.start")
    assert_position(value.get("end"), f"{label}.end")


def assert_diagnostics(message: dict[str, Any], nonempty: bool, version: int | None = None) -> None:
    """Check publishDiagnostics and the shape of every entry."""
    params = message.get("params")
    require(isinstance(params, dict), "diagnostics params are missing")
    diagnostics = params.get("diagnostics")
    require(params.get("version") == version if version is not None else "version" not in params,
            f"unexpected diagnostics version: {params.get('version')!r}")
    require(isinstance(diagnostics, list), "diagnostics is not an array")
    require(bool(diagnostics) == nonempty, f"unexpected diagnostics: {diagnostics!r}")
    for index, diagnostic in enumerate(diagnostics):
        label = f"diagnostics[{index}]"
        require(isinstance(diagnostic, dict), f"{label} is not an object")
        assert_range(diagnostic.get("range"), f"{label}.range")
        severity = diagnostic.get("severity")
        require(type(severity) is int and 1 <= severity <= 4, f"{label}.severity is invalid")
        require(diagnostic.get("source") == "mach", f"{label}.source is invalid")
        require(bool(diagnostic.get("message")), f"{label}.message is empty")


def write_project(parent: Path, project_id: str, value: int) -> tuple[Path, Path, str]:
    """Create a temporary dependency-free Mach project."""
    root = parent / project_id
    source = root / "src"
    source.mkdir(parents=True)
    (root / "mach.toml").write_text(
        f"""[project]
id = "{project_id}"
version = "0.1.0"
src = "src"
out = "out/{{target.name}}/{{profile.name}}"

[target.linux-x86_64]
isa = "x86_64"
os = "linux"
abi = "sysv64"

[profile.debug]
opt = 0
debug = true
simd = "scalarize"

[artifact.app]
kind = "bin"
entry = "main.mach"
out = "bin/app"
targets = ["*"]
link = []
need = []
""",
        encoding="utf-8",
    )
    alias = "vals" if project_id == "beta" else "rootmod"
    import_line = (f"use {alias}: {project_id}.defs;\n"
                   f"use direct: {project_id}.defs.answer;\n"
                   f"use forwarded: {project_id}.bridge.answer;\n"
                   f"use {project_id}.defs.Box;\n"
                   f"use {project_id}.defs.take;\n"
                   f"use {project_id}.defs.watched;")
    text = f"""{import_line}

pub fun main() i32 {{
    var b: Box[i32];
    b.v = {value};
    ret take[i32](b) + direct + {alias}.answer + forwarded + watched;
}}
"""
    main = source / "main.mach"
    definition = source / "defs.mach"
    main.write_text(text, encoding="utf-8")
    definition.write_text(
        f'''$if ($project.target.os != "linux") {{ $error("wrong selected target"); }}
$if ($bin.name != "app") {{ $error("wrong selected artifact"); }}
pub val answer: i32 = {value};
pub val watched: i32 = {value};
pub rec Box[T] {{ v: T; }}
pub fun take[T](b: Box[T]) i32 {{ ret 7; }}
''',
        encoding="utf-8",
    )
    (source / "bridge.mach").write_text(
        f"fwd {project_id}.defs.answer;\n", encoding="utf-8",
    )
    return main, definition, text


def write_vendored_project(parent: Path) -> tuple[Path, Path, str, str]:
    """Create an app with a current-syntax vendored path dependency."""
    root = parent / "vendor-app"
    source = root / "src"
    dep_root = root / "dep" / "vendorlib"
    dep_source = dep_root / "src"
    source.mkdir(parents=True)
    dep_source.mkdir(parents=True)
    (root / "mach.toml").write_text(
        """[project]
id = "vendorapp"
version = "0.1.0"
src = "src"
out = "out/{target.name}/{profile.name}"

[target.linux]
isa = "x86_64"
os = "linux"
abi = "sysv64"

[profile.debug]
opt = 0
debug = true
simd = "scalarize"

[artifact.app]
kind = "bin"
entry = "main.mach"
out = "bin/app"
targets = ["*"]
link = []
need = []

[dep.vendorlib]
path = "dep/vendorlib"
""",
        encoding="utf-8",
    )
    (dep_root / "mach.toml").write_text(
        """[project]
id = "vendorlib"
version = "0.1.0"
src = "./src"
out = "out/{target.name}/{profile.name}"

[artifact.lib]
kind = "static"
entry = "defs.mach"
out = "lib/vendorlib"
targets = ["*"]
link = []
need = []
""",
        encoding="utf-8",
    )
    main_text = "use vendorlib.defs.live;\npub fun main() i32 { ret live::i32; }\n"
    disk_dep_text = "pub val stable: i32 = 1;\n"
    live_dep_text = disk_dep_text + "pub val live: i64 = 77;\n"
    main = source / "main.mach"
    dep = dep_source / "defs.mach"
    main.write_text(main_text, encoding="utf-8")
    dep.write_text(disk_dep_text, encoding="utf-8")
    return main, dep, main_text, live_dep_text


def assert_definition(session: LspSession, main: Path, definition: Path, text: str) -> None:
    """Check that `answer` resolves into the expected project root."""
    lines = text.splitlines()
    line = next(index for index, value in enumerate(lines) if " + direct + " in value)
    response = session.request(
        "textDocument/definition",
        {
            "textDocument": {"uri": main.as_uri()},
            "position": {"line": line, "character": lines[line].index("direct") + 1},
        },
    )
    result = response.get("result")
    require(isinstance(result, dict), f"definition is not a Location: {result!r}")
    require(result.get("uri") == definition.as_uri(), f"definition escaped its root: {result!r}")
    assert_range(result.get("range"), "definition.range")


def definition_after(session: LspSession, path: Path, text: str, prefix: str) -> dict[str, Any]:
    """Request definition of the member immediately following prefix."""
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if prefix in value and "use " not in value)
    character = lines[line].index(prefix) + len(prefix) + 1
    response = session.request(
        "textDocument/definition",
        {"textDocument": {"uri": path.as_uri()},
         "position": {"line": line, "character": character}},
    )
    result = response.get("result")
    require(isinstance(result, dict), f"definition after {prefix!r} is not a Location: {result!r}")
    return result


def definition(session: LspSession, path: Path, text: str, name: str) -> dict[str, Any]:
    """Request a definition at the last occurrence of name."""
    lines = text.splitlines()
    line = next(index for index in range(len(lines) - 1, -1, -1) if name in lines[index])
    response = session.request(
        "textDocument/definition",
        {"textDocument": {"uri": path.as_uri()},
         "position": {"line": line, "character": lines[line].index(name) + 1}},
    )
    result = response.get("result")
    require(isinstance(result, dict), f"definition for {name} is not a Location: {result!r}")
    return result


def run_smoke(server: Path, timeout: float) -> tuple[tuple[int, float, int], list[tuple[str, float]]]:
    """Run lifecycle, diagnostics, synchronization, and multi-root coverage."""
    with tempfile.TemporaryDirectory(prefix="mls-protocol-") as directory:
        root = Path(directory).resolve()
        alpha = write_project(root, "alpha", 11)
        alpha_manifest_path = alpha[0].parents[1] / "mach.toml"
        alpha_manifest_path.write_text(
            alpha_manifest_path.read_text(encoding="utf-8").replace('src = "src"', 'src = "./src"'),
            encoding="utf-8",
        )
        beta = write_project(root, "beta", 22)
        shared_left = write_project(root / "left", "shared", 31)
        shared_right = write_project(root / "right", "shared", 41)
        nested = write_project(alpha[0].parent / "nested", "nested", 51)
        vendored = write_vendored_project(root)
        scratch_uri = (root / "scratch.mach").as_uri()
        session = LspSession(server, root, timeout)
        finished = False
        try:
            response = session.request(
                "initialize",
                {"processId": os.getpid(), "rootUri": root.as_uri(),
                 "capabilities": {"workspace": {"didChangeWatchedFiles": {
                     "dynamicRegistration": True}}}},
            )
            result = response.get("result")
            require(isinstance(result, dict), f"invalid initialize result: {result!r}")
            capabilities = result.get("capabilities")
            require(isinstance(capabilities, dict), "initialize capabilities are missing")
            require(capabilities.get("textDocumentSync") == 2, "incremental sync is not advertised")
            session.notify("initialized", {})
            registration = session.wait_for(
                lambda item: item.get("method") == "client/registerCapability",
                "dynamic watcher registration",
            )
            session.respond_error(registration, -32601, "watch registration rejected")

            session.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": scratch_uri,
                        "languageId": "mach",
                        "version": 1,
                        "text": "pub fun broken(",
                    }
                },
            )
            assert_diagnostics(session.diagnostics(scratch_uri, 1), True, 1)
            session.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": scratch_uri, "version": 2},
                    "contentChanges": [{"text": "pub fun fixed() i32 { ret 0; }\n"}],
                },
            )
            assert_diagnostics(session.diagnostics(scratch_uri, 2), False, 2)
            session.notify("textDocument/didClose", {"textDocument": {"uri": scratch_uri}})
            assert_diagnostics(session.diagnostics(scratch_uri, None), False)

            for main, _, text in (alpha, beta):
                session.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": main.as_uri(),
                            "languageId": "mach",
                            "version": 1,
                            "text": text,
                        }
                    },
                )
                assert_diagnostics(session.diagnostics(main.as_uri(), 1), False, 1)

            # documentSymbol is syntax-only: it must succeed without materializing
            # a compiler root, even while that root's manifest cannot load.
            alpha_manifest = alpha[0].parents[1] / "mach.toml"
            alpha_manifest_text = alpha_manifest.read_text(encoding="utf-8")
            alpha_manifest.write_text(alpha_manifest_text + "\n[broken\n", encoding="utf-8")
            started = time.monotonic()
            symbols = session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": alpha[0].as_uri()}},
            )
            require(time.monotonic() - started < 1.0,
                    "syntax-only documentSymbol blocked on project analysis")
            require(isinstance(symbols.get("result"), list) and symbols["result"],
                    f"documentSymbol depended on project loading: {symbols!r}")
            alpha_manifest.write_text(alpha_manifest_text, encoding="utf-8")

            assert_definition(session, *alpha)
            for result in (
                definition(session, alpha[0], alpha[2], "direct"),
                definition(session, alpha[0], alpha[2], "forwarded"),
                definition_after(session, alpha[0], alpha[2], "rootmod."),
            ):
                require(result.get("uri") == alpha[1].as_uri(),
                        f"alias/re-export definition missed canonical declaration: {result!r}")
            assert_definition(session, *beta)
            assert_definition(session, *alpha)
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": nested[0].as_uri(), "languageId": "mach",
                                  "version": 1, "text": nested[2]}},
            )
            assert_diagnostics(session.diagnostics(nested[0].as_uri(), 1), False, 1)
            assert_definition(session, *nested)
            assert_definition(session, *alpha)
            for main, _, text in (shared_left, shared_right):
                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                      "version": 1, "text": text}},
                )
                assert_diagnostics(session.diagnostics(main.as_uri(), 1), False, 1)
            assert_definition(session, *shared_left)
            assert_definition(session, *shared_right)
            assert_definition(session, *shared_left)
            shared_left_v2 = shared_left[2] + "\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": shared_left[0].as_uri(), "version": 2},
                 "contentChanges": [{"text": shared_left_v2}]},
            )
            session.diagnostics(shared_left[0].as_uri(), 2)
            assert_definition(session, shared_left[0], shared_left[1], shared_left_v2)
            assert_definition(session, *shared_right)
            assert_definition(session, shared_left[0], shared_left[1], shared_left_v2)

            # A rejected dynamic registration must leave manifest fingerprint
            # fallback active: a broken manifest disables the snapshot, and a
            # restored one is retried without a watcher notification.
            alpha_manifest = alpha[0].parents[1] / "mach.toml"
            manifest_text = alpha_manifest.read_text(encoding="utf-8")
            alpha_manifest.write_text(manifest_text + "\n[broken\n", encoding="utf-8")
            # the fingerprint fallback coalesces to at most one scan per 250 ms
            # per root, so a request issued inside that window is answered from
            # the still-live snapshot. wait past it, or this asserts nothing.
            time.sleep(0.4)
            broken = session.request(
                "textDocument/definition",
                {"textDocument": {"uri": alpha[0].as_uri()},
                 "position": {"line": 3, "character": 9}},
            )
            require(broken.get("result") is None,
                    f"watcher rejection disabled manifest fallback: {broken!r}")
            alpha_manifest.write_text(manifest_text, encoding="utf-8")
            # and again on the way back: a failed root retries on the next
            # fingerprint scan, not on the next request
            time.sleep(0.4)
            assert_definition(session, *alpha)

            # An unsaved export change in one module must be visible from another
            # open module through the retained compiler snapshot, not editor fallback.
            alpha_main, alpha_def, alpha_text = alpha
            defs_v1 = alpha_def.read_text(encoding="utf-8")
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": alpha_def.as_uri(), "languageId": "mach",
                                  "version": 1, "text": defs_v1}},
            )
            session.diagnostics(alpha_def.as_uri(), 1)
            defs_v2 = defs_v1 + "pub val live: i32 = 33;\n"
            main_v2 = alpha_text.replace(" + direct + ", " + direct + live + ").replace(
                "use direct: alpha.defs.answer;",
                "use direct: alpha.defs.answer;\nuse alpha.defs.live;")
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": alpha_def.as_uri(), "version": 2},
                 "contentChanges": [{"text": defs_v2}]},
            )
            session.diagnostics(alpha_def.as_uri(), 2)
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": alpha_main.as_uri(), "version": 2},
                 "contentChanges": [{"text": main_v2}]},
            )
            # the change itself publishes every open document of the affected
            # root at its own version: diagnostics follow document state, and
            # the analysis they need is driven here rather than by whichever
            # feature request happens to come next
            assert_diagnostics(session.diagnostics(alpha_main.as_uri(), 2), False, 2)
            assert_diagnostics(session.diagnostics(alpha_def.as_uri(), 2), False, 2)
            session.quiet_diagnostics()
            live_result = definition(session, alpha_main, main_v2, "live")
            require(live_result.get("uri") == alpha_def.as_uri(),
                    f"unsaved imported export did not resolve: {live_result!r}")
            republished = session.quiet_diagnostics()
            require(not republished,
                    f"a feature request republished diagnostics: {republished!r}")

            broken_text = main_v2 + "\nuse alpha.missing.nope;\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": alpha_main.as_uri(), "version": 3},
                 "contentChanges": [{"text": broken_text}]},
            )
            session.diagnostics(alpha_main.as_uri(), 3)
            broken_overlay = session.request(
                "textDocument/definition",
                {"textDocument": {"uri": alpha_main.as_uri()},
                 "position": {"line": 8, "character": 30}},
            )
            require(broken_overlay.get("result") is None,
                    f"invalid unsaved import unexpectedly analyzed: {broken_overlay!r}")
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": alpha_main.as_uri(), "version": 4},
                 "contentChanges": [{"text": main_v2}]},
            )
            session.diagnostics(alpha_main.as_uri(), 4)
            require(definition(session, alpha_main, main_v2, "live").get("uri") == alpha_def.as_uri(),
                    "failed snapshot did not retry after the next unsaved revision")

            session.notify("textDocument/didClose", {"textDocument": {"uri": alpha_def.as_uri()}})
            assert_diagnostics(session.diagnostics(alpha_def.as_uri(), None), False)
            after_close = session.request(
                "textDocument/definition",
                {"textDocument": {"uri": alpha_main.as_uri()},
                 "position": {"line": 5, "character": 35}},
            )
            require(after_close.get("result") is None,
                    f"closed unsaved export remained authoritative: {after_close!r}")
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": alpha_main.as_uri(), "version": 5},
                 "contentChanges": [{"text": alpha_text}]},
            )
            assert_diagnostics(session.diagnostics(alpha_main.as_uri(), 5), False, 5)
            assert_definition(session, alpha_main, alpha_def, alpha_text)

            # A dependency opened before its ancestor graph is loaded must still
            # enter that graph through a filesystem overlay. Current `[dep.*]`
            # routing, a raw `src = "./src"` spelling, sema, and read-only rename
            # are all exercised by the unsaved i64 export.
            vendor_main, vendor_dep, vendor_text, vendor_live = vendored
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": vendor_dep.as_uri(), "languageId": "mach",
                                  "version": 1, "text": vendor_live}},
            )
            assert_diagnostics(session.diagnostics(vendor_dep.as_uri(), 1), False, 1)
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": vendor_main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": vendor_text}},
            )
            assert_diagnostics(session.diagnostics(vendor_main.as_uri(), 1), False, 1)
            vendor_definition = definition(session, vendor_main, vendor_text, "live")
            require(vendor_definition.get("uri") == vendor_dep.as_uri(),
                    f"vendored unsaved export did not resolve: {vendor_definition!r}")
            vendor_lines = vendor_text.splitlines()
            vendor_line = next(i for i, value in enumerate(vendor_lines)
                               if "live" in value and "use " not in value)
            vendor_char = vendor_lines[vendor_line].index("live") + 1
            vendor_hover = session.request(
                "textDocument/hover",
                {"textDocument": {"uri": vendor_main.as_uri()},
                 "position": {"line": vendor_line, "character": vendor_char}},
            )
            require("i64" in json.dumps(vendor_hover.get("result")),
                    f"vendored overlay did not participate in sema: {vendor_hover!r}")
            vendor_rename = session.request(
                "textDocument/rename",
                {"textDocument": {"uri": vendor_main.as_uri()},
                 "position": {"line": vendor_line, "character": vendor_char}, "newName": "changed"},
            )
            changes = vendor_rename.get("result", {}).get("changes")
            require(changes == {}, f"vendored dependency rename was writable: {vendor_rename!r}")
            session.notify("textDocument/didClose", {"textDocument": {"uri": vendor_main.as_uri()}})
            session.notify("textDocument/didClose", {"textDocument": {"uri": vendor_dep.as_uri()}})

            # Standalone buffers retain the upstream editor feature path.
            standalone = root / "standalone.mach"
            standalone_text = "pub val item: i32 = 1;\npub fun get() i32 { ret item; }\n"
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": standalone.as_uri(), "languageId": "mach",
                                  "version": 1, "text": standalone_text}},
            )
            session.diagnostics(standalone.as_uri(), 1)
            stand_def = definition(session, standalone, standalone_text, "item")
            require(stand_def.get("uri") == standalone.as_uri(),
                    f"standalone definition failed: {stand_def!r}")
            symbol_response = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": standalone.as_uri()}},
            )
            require(isinstance(symbol_response.get("result"), list) and symbol_response["result"],
                    f"standalone document symbols failed: {symbol_response!r}")
            completion = session.request(
                "textDocument/completion",
                {"textDocument": {"uri": standalone.as_uri()},
                 "position": {"line": 1, "character": 30}},
            )
            completion_result = completion.get("result")
            require(isinstance(completion_result, dict)
                    and isinstance(completion_result.get("items"), list)
                    and completion_result["items"],
                    f"standalone completion failed: {completion!r}")
            session.notify("textDocument/didClose", {"textDocument": {"uri": standalone.as_uri()}})
            assert_diagnostics(session.diagnostics(standalone.as_uri(), None), False)

            for main, _, _ in (alpha, beta):
                session.notify("textDocument/didClose", {"textDocument": {"uri": main.as_uri()}})
            for main, _, _ in (shared_left, shared_right):
                session.notify("textDocument/didClose", {"textDocument": {"uri": main.as_uri()}})
            session.notify("textDocument/didClose", {"textDocument": {"uri": nested[0].as_uri()}})

            telemetry = session.finish()
            finished = True
            return telemetry, session.timings
        finally:
            if not finished:
                session.abort()


def run_import_navigation(server: Path, timeout: float) -> None:
    """A `use` / `fwd` path must navigate like a body reference to the same symbol.

    An import path is neither an expression nor a type and an import declaration
    has no name span, so nothing in the offset pivot reached it: hover and
    definition both answered null anywhere on a `use` line. The resolver does
    record the bound symbol on the declaration, which is what makes this
    answerable.
    """
    with tempfile.TemporaryDirectory(prefix="mls-import-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "imp", 5)
        bridge = main.parent / "bridge.mach"
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            lines = text.splitlines()

            def at(needle: str, within: str) -> dict[str, Any]:
                line = next(i for i, v in enumerate(lines) if within in v)
                return {"textDocument": {"uri": main.as_uri()},
                        "position": {"line": line, "character": lines[line].index(needle) + 1}}

            def location(label: str, params: dict[str, Any]) -> dict[str, Any]:
                response = session.request("textDocument/definition", params)
                result = response.get("result")
                require(isinstance(result, dict),
                        f"{label}: definition on an import is not a Location: {result!r}")
                assert_range(result.get("range"), f"{label}.range")
                return result

            def hover_text(label: str, params: dict[str, Any]) -> str:
                response = session.request("textDocument/hover", params)
                result = response.get("result")
                require(isinstance(result, dict), f"{label}: hover on an import is null")
                contents = result.get("contents")
                require(isinstance(contents, dict), f"{label}: hover contents malformed")
                return str(contents.get("value"))

            # a plain symbol import: the leaf names a declaration in another module
            leaf = at("Box", "use imp.defs.Box;")
            require(location("symbol import leaf", leaf).get("uri") == defs.as_uri(),
                    "a symbol import leaf did not resolve to its declaring module")
            require("Box" in hover_text("symbol import leaf", leaf),
                    "hover on a symbol import leaf did not name the symbol")

            # the qualifier of the same path resolves to the same symbol, so a
            # cursor anywhere on the line is useful rather than only on the leaf
            require(location("import qualifier", at("imp", "use imp.defs.Box;")).get("uri")
                    == defs.as_uri(),
                    "the qualifier of an import path did not resolve")

            # a member alias binds the imported symbol under a new name
            require(location("member alias", at("direct", "use direct:")).get("uri")
                    == defs.as_uri(),
                    "a member alias did not resolve to its declaration")

            # a bare-module alias names a FILE, not a declaration
            module_alias = at("rootmod", "use rootmod:")
            require(location("module alias", module_alias).get("uri") == defs.as_uri(),
                    "a module alias did not resolve to the module's file")
            require("module" in hover_text("module alias", module_alias),
                    "hover on a module alias did not name it as a module")

            # `fwd` re-export paths behave like `use` paths
            bridge_text = bridge.read_text(encoding="utf-8")
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": bridge.as_uri(), "languageId": "mach",
                                  "version": 1, "text": bridge_text}},
            )
            session.diagnostics(bridge.as_uri(), 1)
            blines = bridge_text.splitlines()
            bline = next(i for i, v in enumerate(blines) if v.startswith("fwd "))
            fwd = {"textDocument": {"uri": bridge.as_uri()},
                   "position": {"line": bline, "character": blines[bline].index("answer") + 1}}
            response = session.request("textDocument/definition", fwd)
            result = response.get("result")
            require(isinstance(result, dict) and result.get("uri") == defs.as_uri(),
                    f"a fwd re-export path did not resolve: {result!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_document_symbol_hierarchy(server: Path, timeout: float) -> None:
    """A record's fields and a function's parameters belong in the outline.

    documentSymbol reported a flat list, so a module's structure was invisible:
    39 top-level names and no way to see what any of them contained.
    """
    with tempfile.TemporaryDirectory(prefix="mls-dsym-") as directory:
        root = Path(directory).resolve()
        main, defs, _ = write_project(root, "dsym", 7)
        defs_text = defs.read_text(encoding="utf-8")
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": defs.as_uri(), "languageId": "mach",
                                  "version": 1, "text": defs_text}},
            )
            session.diagnostics(defs.as_uri(), 1)
            response = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            symbols = response.get("result")
            require(isinstance(symbols, list) and symbols,
                    f"documentSymbol returned nothing: {symbols!r}")

            by_name = {s["name"]: s for s in symbols}

            def children(name: str) -> list[dict[str, Any]]:
                require(name in by_name, f"{name} missing from documentSymbol")
                node = by_name[name]
                assert_range(node.get("range"), f"{name}.range")
                assert_range(node.get("selectionRange"), f"{name}.selectionRange")
                return node.get("children") or []

            # `pub rec Box[T] { v: T; }` -- one generic and one field
            box = children("Box")
            names = [c["name"] for c in box]
            require("T" in names, f"Box did not report its generic parameter: {names!r}")
            require("v" in names, f"Box did not report its field: {names!r}")
            field = next(c for c in box if c["name"] == "v")
            require(field.get("detail") == "T",
                    f"a field's declared type is missing from detail: {field!r}")
            require(field.get("kind") == 8, f"a record field is not SymbolKind.Field: {field!r}")
            for entry in box:
                assert_range(entry.get("range"), "Box child range")
                assert_range(entry.get("selectionRange"), "Box child selectionRange")

            # `pub fun take[T](b: Box[T]) i32` -- one generic and one parameter
            take = children("take")
            take_names = [c["name"] for c in take]
            require("T" in take_names, f"take did not report its generic: {take_names!r}")
            require("b" in take_names, f"take did not report its parameter: {take_names!r}")
            param = next(c for c in take if c["name"] == "b")
            require(param.get("detail") == "Box[T]",
                    f"a parameter's declared type is missing from detail: {param!r}")

            # a declaration with no members omits children rather than sending []
            require("children" not in by_name["answer"] or not by_name["answer"]["children"],
                    "a val reported children it does not have")

            # The outline reuses the buffer's cached parse rather than re-parsing
            # per request, which is only correct while an edit drops that cache.
            # A stale outline is silent - it looks like a working feature naming
            # symbols that are no longer there - so the invalidation is pinned.
            edited = defs_text + "\npub fun freshly_added(q: i32) i32 { ret q; }\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": defs.as_uri(), "version": 2},
                 "contentChanges": [{"text": edited}]},
            )
            session.diagnostics(defs.as_uri(), 2)
            after = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            after_names = [s["name"] for s in (after.get("result") or [])]
            require("freshly_added" in after_names,
                    f"documentSymbol served a stale parse after an edit: {after_names!r}")
            reissued = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            require([s["name"] for s in (reissued.get("result") or [])] == after_names,
                    "documentSymbol is not stable across identical requests")

            # and a decl removed by an edit must leave the outline
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": defs.as_uri(), "version": 3},
                 "contentChanges": [{"text": defs_text}]},
            )
            session.diagnostics(defs.as_uri(), 3)
            reverted = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            reverted_names = [s["name"] for s in (reverted.get("result") or [])]
            require("freshly_added" not in reverted_names,
                    f"a removed declaration survived in the outline: {reverted_names!r}")

            # still syntax-only: it must answer without a compiler root
            manifest = defs.parents[1] / "mach.toml"
            manifest_text = manifest.read_text(encoding="utf-8")
            manifest.write_text(manifest_text + "\n[broken\n", encoding="utf-8")
            time.sleep(0.4)
            started = time.monotonic()
            broken = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            require(time.monotonic() - started < 1.0,
                    "documentSymbol blocked on project analysis")
            require(isinstance(broken.get("result"), list) and broken["result"],
                    f"documentSymbol needed a loaded project: {broken!r}")
            manifest.write_text(manifest_text, encoding="utf-8")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_completion_context(server: Path, timeout: float) -> None:
    """Completion must answer for the cursor, not for the file.

    The server advertises `.` as a trigger character, and typing a dot used to
    return every top-level name in the file with none of the receiver's members
    among them -- a wrong answer rather than a missing one. Nor did a partial
    identifier narrow anything.
    """
    with tempfile.TemporaryDirectory(prefix="mls-compl-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "compl", 3)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            lines = text.splitlines()
            # after `b.v = N;`, so the local `b` is in scope for the probe
            anchor_line = next(i for i, v in enumerate(lines) if v.strip().startswith("b.v ="))
            version = [1]

            def complete(probe: str) -> list[str]:
                """Insert `probe` as a line in main's body and complete at its end."""
                edited = list(lines)
                edited.insert(anchor_line + 1, "    " + probe)
                version[0] += 1
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version[0]},
                     "contentChanges": [{"text": "\n".join(edited) + "\n"}]},
                )
                session.diagnostics(main.as_uri(), version[0])
                response = session.request(
                    "textDocument/completion",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": anchor_line + 1, "character": 4 + len(probe)}},
                )
                result = response.get("result")
                require(isinstance(result, dict), f"completion is not a list: {result!r}")
                items = result.get("items")
                require(isinstance(items, list), f"completion has no items: {result!r}")
                for item in items:
                    require(isinstance(item.get("label"), str), f"item without a label: {item!r}")
                    require(isinstance(item.get("kind"), int), f"item without a kind: {item!r}")
                return [item["label"] for item in items]

            # a record receiver offers its fields, and only its fields
            fields = complete("b.")
            require("v" in fields, f"a record receiver did not offer its field: {fields!r}")
            require("main" not in fields and "take" not in fields,
                    f"a record receiver offered file-level names: {fields!r}")

            # a module alias offers that module's public symbols
            members = complete("rootmod.")
            require("answer" in members and "Box" in members,
                    f"a module alias did not offer its exports: {members!r}")
            require("main" not in members,
                    f"a module alias offered the requesting file's names: {members!r}")

            # a partial member narrows
            narrowed = complete("rootmod.an")
            require(narrowed and all(label.startswith("an") for label in narrowed),
                    f"a partial member name did not filter: {narrowed!r}")
            require("answer" in narrowed, f"filtering dropped the match: {narrowed!r}")

            # an unresolvable receiver offers nothing, never the file's names
            unknown = complete("nosuchreceiver.")
            require(unknown == [],
                    f"an unresolved receiver fell back to the file list: {unknown!r}")

            # a partial identifier with no dot narrows the file-level list
            everything = complete("")
            prefixed = complete("Bo")
            require(prefixed and all(label.startswith("Bo") for label in prefixed),
                    f"a partial identifier did not filter: {prefixed!r}")
            require(len(prefixed) < len(everything),
                    "filtering returned as many items as no filter at all")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_document_highlight(server: Path, timeout: float) -> None:
    """Occurrences in the active file, classified read or write."""
    with tempfile.TemporaryDirectory(prefix="mls-hl-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "imp", 4)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            lines = text.splitlines()

            def highlights(needle: str, within: str) -> list[dict[str, Any]]:
                line = next(i for i, v in enumerate(lines) if within in v)
                response = session.request(
                    "textDocument/documentHighlight",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": line, "character": lines[line].index(needle) + 1}},
                )
                items = response.get("result")
                require(isinstance(items, list), f"documentHighlight is not a list: {items!r}")
                for item in items:
                    assert_range(item.get("range"), "highlight.range")
                    require(item.get("kind") in (1, 2, 3),
                            f"highlight kind is not a DocumentHighlightKind: {item!r}")
                    require("uri" not in item,
                            f"a highlight carried a uri, so it is a Location: {item!r}")
                return items

            # a top-level declaration: its own name is a write, its uses reads
            found = highlights("watched", "use imp.defs.watched;")
            require(found, f"an import was not highlighted: {found!r}")

            # an imported symbol used in the body
            uses = highlights("take", "ret take")
            require(uses, "an imported symbol produced no highlight")
            require({item["kind"] for item in uses} <= {1, 2, 3},
                    f"unexpected highlight kinds: {uses!r}")

            # a cursor on nothing answers an empty list, not an error
            blank = session.request(
                "textDocument/documentHighlight",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": 0, "character": 0}},
            )
            require(isinstance(blank.get("result"), list),
                    f"a cursor on nothing did not answer a list: {blank!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_workspace_symbol(server: Path, timeout: float) -> None:
    """Find a declaration without already looking at it."""
    with tempfile.TemporaryDirectory(prefix="mls-wsym-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "wsym", 6)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            require(result.get("capabilities", {}).get("workspaceSymbolProvider") is True,
                    "workspaceSymbolProvider is not advertised")
            session.notify("initialized", {})

            def query(text_: str) -> list[dict[str, Any]]:
                response = session.request("workspace/symbol", {"query": text_})
                items = response.get("result")
                require(isinstance(items, list), f"workspace/symbol is not a list: {items!r}")
                for item in items:
                    require(isinstance(item.get("name"), str), f"symbol without a name: {item!r}")
                    require(isinstance(item.get("kind"), int), f"symbol without a kind: {item!r}")
                    location = item.get("location")
                    require(isinstance(location, dict) and "uri" in location,
                            f"symbol without a location: {item!r}")
                    assert_range(location.get("range"), "symbol.location.range")
                return items

            # nothing is loaded yet: a query must answer, not block on a build
            started = time.monotonic()
            require(query("answer") == [], "an unloaded workspace returned symbols")
            require(time.monotonic() - started < 2.0,
                    "workspace/symbol forced a cold project load")

            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            found = query("answer")
            require(found, "a declared symbol was not found after loading")
            names = [item["name"] for item in found]
            require("answer" in names, f"the exact match is missing: {names!r}")
            hit = next(item for item in found if item["name"] == "answer")
            require(hit["location"]["uri"] == defs.as_uri(),
                    f"symbol resolved to the wrong file: {hit!r}")
            require(hit.get("containerName"), "no containerName to disambiguate the module")

            # a leading match outranks an interior one
            ranked = [item["name"] for item in query("Box")]
            require(ranked and ranked[0] == "Box",
                    f"an exact match was not ranked first: {ranked!r}")

            require(query("zzz-no-such-symbol") == [], "a miss returned results")
            require(query("") == [], "an empty query returned results")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_signature_help(server: Path, timeout: float) -> None:
    """Parameter hints while the argument list is still incomplete."""
    with tempfile.TemporaryDirectory(prefix="mls-sig-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "sig", 8)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            provider = result.get("capabilities", {}).get("signatureHelpProvider")
            require(isinstance(provider, dict) and "(" in provider.get("triggerCharacters", []),
                    f"signatureHelpProvider is not advertised with `(`: {provider!r}")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            lines = text.splitlines()
            anchor_line = next(i for i, v in enumerate(lines) if v.strip().startswith("b.v ="))
            version = [1]

            def help_at(probe: str) -> dict[str, Any] | None:
                edited = list(lines)
                edited.insert(anchor_line + 1, "    " + probe)
                version[0] += 1
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version[0]},
                     "contentChanges": [{"text": "\n".join(edited) + "\n"}]},
                )
                session.diagnostics(main.as_uri(), version[0])
                response = session.request(
                    "textDocument/signatureHelp",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": anchor_line + 1, "character": 4 + len(probe)}},
                )
                return response.get("result")

            # the argument list is unclosed at every one of these positions
            opened = help_at("take[i32](")
            require(opened, "signatureHelp gave nothing for an open call")
            signature = opened["signatures"][0]
            require("b" in signature["label"],
                    f"the parameter is missing from the label: {signature['label']!r}")
            require(opened.get("activeParameter") == 0,
                    f"the first argument is not active: {opened!r}")
            require(len(signature.get("parameters") or []) == 1,
                    f"parameter list is wrong: {signature!r}")
            # each parameter label is a byte range into the signature label
            span = signature["parameters"][0]["label"]
            require(isinstance(span, list) and len(span) == 2 and span[0] < span[1],
                    f"parameter label is not a valid range: {span!r}")
            require(signature["label"][span[0]:span[1]].startswith("b"),
                    f"parameter range does not cover the parameter: {signature!r}")

            # a `(` inside a string literal must not open a call
            quoted = help_at('take[i32]("a(b"')
            require(quoted, "a paren inside a string broke the enclosing call")

            # a cursor outside any call, and a callee that resolves to nothing
            require(help_at("val zz: i64 = 1;") is None,
                    "signatureHelp answered outside a call")
            require(help_at("no_such_function(") is None,
                    "signatureHelp answered for an unresolvable callee")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_inlay_hints(server: Path, timeout: float) -> None:
    """Parameter names on literal arguments, and nowhere else.

    Mach requires an explicit type annotation on every binding, so there is no
    inferred binding type to reveal; what is opaque at a call site is which
    literal means what.
    """
    with tempfile.TemporaryDirectory(prefix="mls-hint-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "hint", 2)
        # a two-parameter callee, called with one literal and one named value
        extra = ("pub fun pair(first: i32, second: i32) i32 { ret first + second; }\n")
        defs.write_text(defs.read_text(encoding="utf-8") + extra, encoding="utf-8")
        body = text.replace("ret take[i32](b)", "ret pair(1, watched) + take[i32](b)")
        body = body.replace("use hint.defs.watched;", "use hint.defs.watched;\nuse hint.defs.pair;")
        main.write_text(body, encoding="utf-8")

        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            require(result.get("capabilities", {}).get("inlayHintProvider") is True,
                    "inlayHintProvider is not advertised")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": body}},
            )
            session.diagnostics(main.as_uri(), 1)

            lines = body.splitlines()
            response = session.request(
                "textDocument/inlayHint",
                {"textDocument": {"uri": main.as_uri()},
                 "range": {"start": {"line": 0, "character": 0},
                           "end": {"line": len(lines), "character": 0}}},
            )
            hints = response.get("result")
            require(isinstance(hints, list), f"inlayHint is not a list: {hints!r}")
            for hint in hints:
                assert_position(hint.get("position"), "hint.position")
                require(isinstance(hint.get("label"), str), f"hint without a label: {hint!r}")
                require(hint.get("kind") == 2, f"hint is not InlayHintKind.Parameter: {hint!r}")

            labels = [hint["label"] for hint in hints]
            require("first:" in labels,
                    f"the literal argument was not named: {labels!r}")
            # `watched` is an identifier, not a literal, so it is left alone
            require("second:" not in labels,
                    f"a self-naming argument was labelled: {labels!r}")

            # a range that covers nothing yields nothing
            empty = session.request(
                "textDocument/inlayHint",
                {"textDocument": {"uri": main.as_uri()},
                 "range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 0}}},
            )
            require(empty.get("result") == [], f"an empty range produced hints: {empty!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_semantic_tokens(server: Path, timeout: float) -> None:
    """Classification from the resolved tables, in a wire format that decodes.

    The payload is a flat array of five-integer groups, each relative to the
    previous, so ordering and non-overlap are load-bearing rather than
    cosmetic: a single out-of-order token corrupts everything after it.
    """
    with tempfile.TemporaryDirectory(prefix="mls-semtok-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "semtok", 5)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            provider = result.get("capabilities", {}).get("semanticTokensProvider")
            require(isinstance(provider, dict), f"semanticTokensProvider missing: {provider!r}")
            legend = provider.get("legend", {})
            types = legend.get("tokenTypes")
            require(isinstance(types, list) and types, f"no token legend: {legend!r}")
            require(isinstance(legend.get("tokenModifiers"), list), "no modifier legend")

            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            response = session.request(
                "textDocument/semanticTokens/full", {"textDocument": {"uri": main.as_uri()}})
            payload = response.get("result")
            require(isinstance(payload, dict), f"semanticTokens is not an object: {payload!r}")
            data = payload.get("data")
            require(isinstance(data, list) and data, f"no token data: {payload!r}")
            require(len(data) % 5 == 0,
                    f"token data is not a multiple of five: {len(data)}")

            lines = text.splitlines()
            line = 0
            char = 0
            previous = (-1, -1)
            seen_types = set()
            for index in range(0, len(data), 5):
                d_line, d_char, length, kind, _mods = data[index:index + 5]
                require(d_line >= 0 and d_char >= 0, f"negative delta at {index}")
                if d_line == 0:
                    char += d_char
                else:
                    line += d_line
                    char = d_char
                require((line, char) >= previous,
                        f"token {index // 5} is out of order at {(line, char)}")
                previous = (line, char)
                require(0 <= kind < len(types), f"token type {kind} outside the legend")
                require(length > 0, f"zero-length token at {index}")
                require(line < len(lines), f"token past end of file at line {line}")
                require(char + length <= len(lines[line]) + 1,
                        f"token runs past end of line {line}")
                seen_types.add(types[kind])

            # the point of the feature: kinds a syntax highlighter cannot infer
            require("type" in seen_types, f"no type tokens: {sorted(seen_types)}")
            require("function" in seen_types, f"no function tokens: {sorted(seen_types)}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_cancellation(server: Path, timeout: float) -> None:
    """A withdrawn request is answered RequestCancelled, never dropped."""
    with tempfile.TemporaryDirectory(prefix="mls-cancel-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "cancel", 3)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            # the cancellation is sent FIRST so this does not race the worker.
            # Against a fixture this small the queue drains faster than a second
            # message arrives, and cancelling work already in progress is
            # best-effort by design; what is under test is that a request found
            # withdrawn when the worker reaches it is answered, not dropped.
            doc = {"textDocument": {"uri": main.as_uri()}}
            first = session.next_id
            session.notify("$/cancelRequest", {"id": first})
            session._send({"jsonrpc": "2.0", "id": first,
                           "method": "textDocument/documentSymbol", "params": doc})
            session.next_id += 1

            answer = session.wait_for(
                lambda item: item.get("id") == first, f"a response for request {first}")
            require("error" in answer,
                    f"a cancelled request was answered normally: {answer!r}")
            require(answer["error"].get("code") == -32800,
                    f"cancellation is not RequestCancelled: {answer!r}")

            # a cancellation naming an id the server never saw must be inert
            session.notify("$/cancelRequest", {"id": 999999})
            later = session.request("textDocument/documentSymbol", doc)
            require(isinstance(later.get("result"), list),
                    f"a stray cancellation disturbed a later request: {later!r}")

            # and the id is consumed: reusing it must not be cancelled again
            reused = session.request("textDocument/documentSymbol", doc)
            require(isinstance(reused.get("result"), list),
                    f"a consumed cancellation still applied: {reused!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_incremental_sync(server: Path, timeout: float) -> None:
    """Range edits patch the buffer, and the buffer is what everything reads."""
    with tempfile.TemporaryDirectory(prefix="mls-incr-") as directory:
        root = Path(directory).resolve()
        main, _, _ = write_project(root, "incr", 1)
        text = "pub fun alpha() i32 { ret 1; }\npub fun beta() i32 { ret 2; }\n"
        main.write_text(text, encoding="utf-8")

        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            require(result.get("capabilities", {}).get("textDocumentSync") == 2,
                    "incremental sync is not advertised")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            version = [1]

            def edit(changes: list[dict[str, Any]]) -> list[str]:
                version[0] += 1
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version[0]},
                     "contentChanges": changes},
                )
                session.diagnostics(main.as_uri(), version[0])
                response = session.request(
                    "textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
                return [s["name"] for s in (response.get("result") or [])]

            def span(l1: int, c1: int, l2: int, c2: int) -> dict[str, Any]:
                return {"start": {"line": l1, "character": c1},
                        "end": {"line": l2, "character": c2}}

            # one range
            names = edit([{"range": span(0, 8, 0, 13), "text": "gamma"}])
            require(names == ["gamma", "beta"], f"single range edit went wrong: {names!r}")

            # two ranges in one notification: the second is expressed against the
            # result of the first, which is how the client computed it
            names = edit([{"range": span(0, 8, 0, 13), "text": "dd"},
                          {"range": span(1, 8, 1, 12), "text": "ee"}])
            require(names == ["dd", "ee"], f"ordered ranges went wrong: {names!r}")

            # a range spanning a line boundary
            names = edit([{"range": span(0, 29, 1, 0), "text": "\n\n"}])
            require(names == ["dd", "ee"], f"a multi-line range went wrong: {names!r}")

            # a full-document change is still accepted in incremental mode
            names = edit([{"text": "pub fun solo() i32 { ret 9; }\n"}])
            require(names == ["solo"], f"a full-text change was mishandled: {names!r}")

            # multi-byte text: the column is UTF-16 code units, not bytes
            names = edit([{"range": span(0, 0, 0, 0), "text": "# \U0001F600 note\n"}])
            require(names == ["solo"], f"a multi-byte insert corrupted the buffer: {names!r}")
            names = edit([{"range": span(0, 5, 0, 9), "text": "x"}])
            require(names == ["solo"],
                    f"an edit after an astral codepoint used byte columns: {names!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_code_actions(server: Path, timeout: float) -> None:
    """Quick fixes come from the compiler's fixes, not from parsing its prose."""
    with tempfile.TemporaryDirectory(prefix="mls-ca-") as directory:
        root = Path(directory).resolve()
        main, _, _ = write_project(root, "ca", 1)
        # a near-miss identifier: the resolver knows the candidate exactly, so
        # the diagnostic carries the replacement as an edit rather than a sentence
        text = ("pub fun helper() i32 { ret 1; }\n"
                "pub fun main() i32 { ret helpr(); }\n")
        main.write_text(text, encoding="utf-8")

        session = LspSession(server, root, timeout)
        finished = False
        try:
            result = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}}).get("result", {})
            provider = result.get("capabilities", {}).get("codeActionProvider")
            require(isinstance(provider, dict)
                    and "quickfix" in provider.get("codeActionKinds", []),
                    f"codeActionProvider is not advertised: {provider!r}")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            published = session.diagnostics(main.as_uri(), 1)
            entries = published["params"]["diagnostics"]
            require(entries, "the misspelling produced no diagnostic")
            target = next((d for d in entries if "helpr" in d["message"]), entries[0])

            def actions(rng: dict[str, Any], only: list[str] | None = None) -> list[dict[str, Any]]:
                ctx: dict[str, Any] = {"diagnostics": [target]}
                if only is not None:
                    ctx["only"] = only
                response = session.request(
                    "textDocument/codeAction",
                    {"textDocument": {"uri": main.as_uri()}, "range": rng, "context": ctx})
                items = response.get("result")
                require(isinstance(items, list), f"codeAction is not a list: {items!r}")
                return items

            found = actions(target["range"])
            require(found, "the diagnostic carried a fix but no action was offered")
            action = found[0]
            require(action.get("kind") == "quickfix", f"wrong kind: {action!r}")
            require("helper" in action.get("title", ""),
                    f"the title does not name the replacement: {action!r}")
            require(action.get("diagnostics"),
                    "the action does not carry its originating diagnostic")

            changes = (action.get("edit") or {}).get("changes") or {}
            edits = changes.get(main.as_uri())
            require(edits, f"no edits for the requested document: {changes!r}")
            for e in edits:
                assert_range(e.get("range"), "edit.range")
                require(isinstance(e.get("newText"), str), f"edit without text: {e!r}")
            require(any(e["newText"] == "helper" for e in edits),
                    f"no edit inserts the candidate: {edits!r}")

            # a cursor is a zero-length range, and the fix under it must still be
            # offered even though nothing is selected
            caret = {"start": target["range"]["start"], "end": target["range"]["start"]}
            require(actions(caret), "a zero-length range offered nothing")

            # an `only` filter naming something this server does not provide
            require(actions(target["range"], ["refactor"]) == [],
                    "an unrelated only-filter still returned quickfixes")
            require(actions(target["range"], ["quickfix"]),
                    "an explicit quickfix filter returned nothing")

            # a range with no diagnostic offers nothing
            empty = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
            require(actions(empty) == [], "a clean range offered actions")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_hover_presentation(server: Path, timeout: float) -> None:
    """Hover renders what the client can read, and types the source never spells."""
    with tempfile.TemporaryDirectory(prefix="mls-hov-") as directory:
        root = Path(directory).resolve()
        main, _, _ = write_project(root, "hov", 1)
        text = ("pub fun twice(n: i32) i32 { ret n + n; }\n"
                "pub rec Pair { a: i32; b: i32; }\n"
                "pub fun main() i32 { ret twice(2) + 1; }\n")
        main.write_text(text, encoding="utf-8")
        lines = text.splitlines()

        def session_with(fmt: list[str] | None) -> LspSession:
            caps: dict[str, Any] = {}
            if fmt is not None:
                caps = {"textDocument": {"hover": {"contentFormat": fmt}}}
            s = LspSession(server, root, timeout)
            s.request("initialize", {"rootUri": root.as_uri(), "capabilities": caps})
            s.notify("initialized", {})
            s.notify("textDocument/didOpen",
                     {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                       "version": 1, "text": text}})
            s.diagnostics(main.as_uri(), 1)
            return s

        def hover(s: LspSession, within: str, needle: str, off: int = 1) -> dict[str, Any] | None:
            line = next(i for i, v in enumerate(lines) if within in v)
            response = s.request(
                "textDocument/hover",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": line, "character": lines[line].index(needle) + off}})
            return response.get("result")

        # a record renders its header, not its fields
        s = session_with(["markdown"])
        finished = False
        try:
            rec = hover(s, "pub rec Pair", "Pair")
            require(rec, "hovering a record gave nothing")
            value = rec["contents"]["value"]
            require(rec["contents"]["kind"] == "markdown", f"wrong kind: {rec!r}")
            require("rec Pair" in value, f"the header is missing: {value!r}")
            require("a: i32" not in value, f"the whole body was rendered: {value!r}")

            # an expression the source never gives a type: a call result
            expr = hover(s, "ret twice(2)", "twice(2)", 0)
            require(expr, "hovering an expression gave nothing")
            require("i32" in expr["contents"]["value"],
                    f"the expression's type is missing: {expr!r}")
            s.finish()
            finished = True
        finally:
            if not finished:
                s.abort()

        # a client that only reads plaintext must not be sent fences
        s = session_with(["plaintext"])
        finished = False
        try:
            plain = hover(s, "pub rec Pair", "Pair")
            require(plain, "hovering gave nothing for a plaintext client")
            contents = plain["contents"]
            require(contents["kind"] == "plaintext", f"wrong kind: {plain!r}")
            require("```" not in contents["value"],
                    f"markdown fences were sent to a plaintext client: {contents!r}")
            require("rec Pair" in contents["value"],
                    f"unfencing lost the content: {contents!r}")
            s.finish()
            finished = True
        finally:
            if not finished:
                s.abort()

        # a client advertising nothing predates the capability; the spec's
        # default there is plaintext
        s = session_with(None)
        finished = False
        try:
            legacy = hover(s, "pub rec Pair", "Pair")
            require(legacy and legacy["contents"]["kind"] == "plaintext",
                    f"a client with no hover capability got markdown: {legacy!r}")
            s.finish()
            finished = True
        finally:
            if not finished:
                s.abort()


def run_active_watcher_fallback(server: Path, timeout: float) -> None:
    """Prove a missed source event is recovered even after watcher ACK."""
    with tempfile.TemporaryDirectory(prefix="mls-watch-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "watch", 9)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request(
                "initialize",
                {"rootUri": root.as_uri(), "capabilities": {"workspace": {
                    "didChangeWatchedFiles": {"dynamicRegistration": True}}}},
            )
            session.notify("initialized", {})
            registration = session.wait_for(
                lambda item: item.get("method") == "client/registerCapability",
                "watch registration",
            )
            session.respond_result(registration)
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            assert_definition(session, main, defs, text)

            changed = defs.read_text(encoding="utf-8").replace(
                "pub val watched: i32", "pub val watched: i64")
            defs.write_text(changed, encoding="utf-8")
            time.sleep(0.3)
            lines = text.splitlines()
            line = next(i for i, value in enumerate(lines) if "watched" in value and "use " not in value)
            hover = session.request(
                "textDocument/hover",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": line, "character": lines[line].index("watched") + 1}},
            )
            require("i64" in json.dumps(hover.get("result")),
                    f"active watcher suppressed source fingerprint fallback: {hover!r}")

            broken = changed + "use watch.missing.nope;\n"
            defs.write_text(broken, encoding="utf-8")
            time.sleep(0.3)
            failed = session.request(
                "textDocument/definition",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": line, "character": lines[line].index("watched") + 1}},
            )
            require(failed.get("result") is None,
                    f"broken on-disk source retained a stale snapshot: {failed!r}")
            defs.write_text(changed, encoding="utf-8")
            time.sleep(0.3)
            repaired = definition(session, main, text, "watched")
            require(repaired.get("uri") == defs.as_uri(),
                    f"failed root did not retry after disk source repair: {repaired!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_same_fqn_reverse(server: Path, timeout: float) -> None:
    """Load colliding project IDs in reverse order and invalidate the first."""
    with tempfile.TemporaryDirectory(prefix="mls-fqn-reverse-") as directory:
        root = Path(directory).resolve()
        left = write_project(root / "left", "shared", 61)
        right = write_project(root / "right", "shared", 71)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            for main, definition_path, text in (right, left):
                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                      "version": 1, "text": text}},
                )
                assert_diagnostics(session.diagnostics(main.as_uri(), 1), False, 1)
                assert_definition(session, main, definition_path, text)
            assert_definition(session, *right)
            right_v2 = right[2] + "\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": right[0].as_uri(), "version": 2},
                 "contentChanges": [{"text": right_v2}]},
            )
            session.diagnostics(right[0].as_uri(), 2)
            assert_definition(session, right[0], right[1], right_v2)
            assert_definition(session, *left)
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_bad_frame(server: Path, frame: bytes, timeout: float, label: str) -> None:
    """Require one malformed or oversized frame to terminate with status 1."""
    with tempfile.TemporaryDirectory(prefix="mls-protocol-bad-") as directory:
        try:
            result = subprocess.run(
                [str(server)],
                cwd=directory,
                input=frame,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ProtocolError(f"{label}: server did not terminate") from error
    require(result.returncode == 1, f"{label}: expected exit 1, got {result.returncode}")
    require(result.stdout == b"", f"{label}: server emitted a partial response")


def run_crash_containment(server: Path, timeout: float) -> None:
    """A worker fault is reported, and the session comes back.

    The compiler front end runs over buffers the user is actively breaking, and
    `std` exposes no way to trap an in-process fault, so the process the editor
    talks to does not run it. Killing the worker stands in for the fault.

    Surviving the fault is not the same as recovering from it. Everything the
    worker knew - which documents are open and what they now contain - died with
    it, and the client will not send any of it again: `didOpen` arrives once, and
    every `didChange` after it is a span against text only the worker kept. So
    the supervisor keeps its own copy and replays it. What is checked here is
    that the replay carries the EDITED text, because replaying the text the file
    was opened with would look identical until the moment it matters.

    The CONTAINMENT is portable; standing in for a fault is not. Finding the
    child needs `pgrep` and killing it needs `SIGKILL`, neither of which exists
    on Windows, so this is skipped there rather than rewritten around a weaker
    signal that would prove something different.
    """
    if os.name != "posix":
        print("  crash containment: skipped (needs pgrep and SIGKILL)")
        return

    def worker_of(session: "LspSession") -> int:
        for _ in range(200):
            children = subprocess.run(["pgrep", "-P", str(session.proc.pid)],
                                      capture_output=True, text=True).stdout.split()
            if children:
                return int(children[0])
            time.sleep(0.02)
        raise AssertionError("no analysis worker: the compiler runs in the client process")

    # a crash is survived, and the session resumes with the text it had
    with tempfile.TemporaryDirectory(prefix="mls-crash-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "crash", 5)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            # the process the client talks to must not be the one analysing
            worker = worker_of(session)

            # an edit the client will never send again: it exists only in the
            # worker's buffer and in whatever the supervisor kept
            edited = text + "\npub fun survived_the_crash(n: i32) i32 { ret n; }\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": main.as_uri(), "version": 2},
                 "contentChanges": [{"text": edited}]},
            )
            session.diagnostics(main.as_uri(), 2)

            pending = session.next_id
            session._send({"jsonrpc": "2.0", "id": pending,
                           "method": "textDocument/references",
                           "params": {"textDocument": {"uri": main.as_uri()},
                                      "position": {"line": 0, "character": 4},
                                      "context": {"includeDeclaration": True}}})
            session.next_id += 1
            os.kill(worker, signal.SIGKILL)

            # The request is answered rather than left hanging. Whether the
            # answer is the crash error or a real result is a race the test
            # cannot win - under load the worker sometimes finishes before the
            # signal lands - and it is not what needs guarding. What needs
            # guarding is that SOMETHING comes back for that id, because the
            # failure this replaces was a client waiting on it forever.
            answer = session.wait_for(
                lambda item: item.get("id") == pending,
                "a response after the worker died")
            require("error" in answer or "result" in answer,
                    f"the request the worker died on was never answered: {answer!r}")

            # and the person is told what happened, as a warning rather than an
            # error, because the session is coming back
            note = session.wait_for(
                lambda item: item.get("method") == "window/showMessage",
                "a message explaining the crash")
            require("crash" in note["params"]["message"].lower(),
                    f"the message does not explain the crash: {note!r}")
            require(note["params"].get("type") == 2,
                    f"a recovered crash was not reported as a warning: {note!r}")

            # the session works again, against the edited text, without the
            # client re-opening anything
            symbols = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            names = [s["name"] for s in (symbols.get("result") or [])]
            require(names, f"the session did not recover: {symbols!r}")
            require("survived_the_crash" in names,
                    f"the replay lost the edits made before the crash: {names!r}")

            # exactly one initialize response reached the client: the replayed
            # worker answers the id the client already holds, and forwarding it
            # would be two responses for one request
            stray = [item for item in session.pending
                     if item.get("id") == 1 and ("result" in item or "error" in item)]
            require(not stray,
                    f"the replayed initialize response was forwarded to the client: {stray!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()

    # a worker that keeps dying is not replaced forever
    with tempfile.TemporaryDirectory(prefix="mls-crashloop-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "loop", 6)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            # each replacement is killed before it answers anything, which is
            # what a worker dying on the session itself looks like
            for _ in range(8):
                try:
                    os.kill(worker_of(session), signal.SIGKILL)
                except (AssertionError, ProcessLookupError):
                    break
                time.sleep(0.15)
                if session.proc.poll() is not None:
                    break

            code = session.proc.wait(timeout=timeout)
            require(code == 3, f"a crash loop exited {code}, want 3")
            finished = True
        finally:
            if not finished:
                session.abort()


def run_hung_worker(server: Path, timeout: float) -> None:
    """Analysis that cannot be interrupted must not hold the session forever.

    A crash at least closes a pipe. A compiler stuck in non-cooperative code
    does not: it holds the request, ignores cancellation because it never
    reaches the point of reading one, and leaves the editor waiting on an id
    that will never come back. Nothing inside the worker can fix that, so the
    supervisor ends the process and treats it as the crash it already knows how
    to recover from.

    SIGSTOP stands in for the wedge. It is a better model than a sleep loop
    because a stopped process really is unable to read its input, which is the
    property that makes a hang unrecoverable from the inside. Like the crash
    test this needs `pgrep` and POSIX signals, so it is skipped elsewhere.
    """
    if os.name != "posix":
        print("  hung worker: skipped (needs pgrep and SIGSTOP)")
        return

    def worker_of(session: "LspSession") -> int:
        for _ in range(200):
            children = subprocess.run(["pgrep", "-P", str(session.proc.pid)],
                                      capture_output=True, text=True).stdout.split()
            if children:
                return int(children[0])
            time.sleep(0.02)
        raise AssertionError("no analysis worker to wedge")

    # a deadline short enough to test, in place of the two minutes a real
    # session allows before it will call analysis stuck
    previous = os.environ.get("MLS_REQUEST_DEADLINE_MS")
    os.environ["MLS_REQUEST_DEADLINE_MS"] = "1200"
    try:
        with tempfile.TemporaryDirectory(prefix="mls-hang-") as directory:
            root = Path(directory).resolve()
            main, _, text = write_project(root, "hang", 5)
            session = LspSession(server, root, timeout)
            finished = False
            wedged = None
            try:
                session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
                session.notify("initialized", {})
                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                      "version": 1, "text": text}},
                )
                session.diagnostics(main.as_uri(), 1)

                edited = text + "\npub fun survived_the_hang(n: i32) i32 { ret n; }\n"
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": 2},
                     "contentChanges": [{"text": edited}]},
                )
                session.diagnostics(main.as_uri(), 2)

                wedged = worker_of(session)
                os.kill(wedged, signal.SIGSTOP)

                pending = session.next_id
                session.next_id += 1
                session._send({"jsonrpc": "2.0", "id": pending,
                               "method": "textDocument/documentSymbol",
                               "params": {"textDocument": {"uri": main.as_uri()}}})

                answer = session.wait_for(
                    lambda item: item.get("id") == pending,
                    "an answer to the request the worker never read")
                error = answer.get("error") or {}
                # ServerCancelled, not InternalError: nothing went wrong inside
                # the request, it was abandoned, and a client is entitled to
                # tell those apart
                require(error.get("code") == -32802,
                        f"a wedged request was not answered ServerCancelled: {answer!r}")

                note = session.wait_for(
                    lambda item: item.get("method") == "window/showMessage",
                    "a message explaining the hang")
                require("responding" in note["params"]["message"].lower(),
                        f"the message does not explain the hang: {note!r}")

                # and the session comes back, still holding the edit
                symbols = session.request(
                    "textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
                names = [s["name"] for s in (symbols.get("result") or [])]
                require("survived_the_hang" in names,
                        f"the session did not recover from the hang: {names!r}")

                session.finish()
                finished = True
            finally:
                if not finished:
                    session.abort()
                if wedged is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(wedged, signal.SIGKILL)

        # shutdown terminates even while analysis is wedged, with the code the
        # protocol asks for: a server that will not exit is one the user has to
        # go and find
        for send_shutdown, expected in ((True, 0), (False, 1)):
            with tempfile.TemporaryDirectory(prefix="mls-downhang-") as directory:
                root = Path(directory).resolve()
                main, _, text = write_project(root, "downhang", 6)
                session = LspSession(server, root, timeout)
                wedged = None
                try:
                    session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
                    session.notify("initialized", {})
                    session.notify(
                        "textDocument/didOpen",
                        {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                          "version": 1, "text": text}},
                    )
                    session.diagnostics(main.as_uri(), 1)

                    wedged = worker_of(session)
                    os.kill(wedged, signal.SIGSTOP)
                    if send_shutdown:
                        session._send({"jsonrpc": "2.0", "id": session.next_id,
                                       "method": "shutdown"})
                        session.next_id += 1
                    session.notify("exit", {})

                    code = session.proc.wait(timeout=timeout)
                    require(code == expected,
                            f"a wedged shutdown exited {code}, want {expected}")
                finally:
                    with contextlib.suppress(Exception):
                        session.abort()
                    if wedged is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(wedged, signal.SIGKILL)
    finally:
        if previous is None:
            os.environ.pop("MLS_REQUEST_DEADLINE_MS", None)
        else:
            os.environ["MLS_REQUEST_DEADLINE_MS"] = previous


def run_progress_reporting(server: Path, timeout: float) -> None:
    """A cold load must look like work, not like a hang.

    Analysis of a project is seconds during which the server answers nothing and
    says nothing, which from the outside is indistinguishable from a server that
    has wedged - and telling those apart matters more now that the supervisor
    waits two minutes before it will call analysis stuck.

    Three things are checked, because each has its own way of being wrong: a
    client that never agreed to progress must not be sent any; a report must
    open and close exactly once for a cold load and not at all for the
    revalidations that follow; and a token whose worker dies must still be
    closed, or the person is left with a spinner that never goes away - the
    visible form of the failure the supervisor exists to clean up after.
    """
    def reports(session: "LspSession") -> list[dict[str, Any]]:
        return [m for m in session.pending if m.get("method") == "$/progress"]

    # a client that did not ask for progress is not sent any
    with tempfile.TemporaryDirectory(prefix="mls-prog-off-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "progoff", 5)
        session = LspSession(server, root, timeout)
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            session.request("textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            require(not reports(session),
                    f"progress was sent to a client that did not advertise it: {reports(session)!r}")
            creates = [m for m in session.pending
                       if m.get("method") == "window/workDoneProgress/create"]
            require(not creates, f"a progress token was created unasked: {creates!r}")
            session.finish()
        finally:
            with contextlib.suppress(Exception):
                session.abort()

    # the cold load reports once; the revalidations after it do not
    with tempfile.TemporaryDirectory(prefix="mls-prog-on-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "progon", 5)
        session = LspSession(server, root, timeout)
        try:
            session.request(
                "initialize",
                {"rootUri": root.as_uri(),
                 "capabilities": {"window": {"workDoneProgress": True}}},
            )
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            for version in (2, 3):
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": text + f"\npub fun e{version}(n: i32) i32 {{ ret n; }}\n"}]},
                )
                session.diagnostics(main.as_uri(), version)
            session.request("textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})

            kinds = [m["params"]["value"]["kind"] for m in reports(session)]
            require(kinds == ["begin", "end"],
                    f"a cold load did not report exactly once: {kinds!r}")
            tokens = {m["params"]["token"] for m in reports(session)}
            require(len(tokens) == 1, f"begin and end used different tokens: {tokens!r}")
            session.finish()
        finally:
            with contextlib.suppress(Exception):
                session.abort()

    # a report whose worker dies is still closed
    if os.name != "posix":
        print("  progress: orphan close skipped (needs pgrep and SIGSTOP)")
        return

    previous = os.environ.get("MLS_REQUEST_DEADLINE_MS")
    os.environ["MLS_REQUEST_DEADLINE_MS"] = "800"
    try:
        with tempfile.TemporaryDirectory(prefix="mls-prog-orphan-") as directory:
            root = Path(directory).resolve()
            main, _, text = write_project(root, "progorphan", 5)
            # a load slow enough to be interrupted part-way. The window scales
            # with how loaded the machine is, and so does the time to stop the
            # worker, so this does not get tighter under CI contention.
            bulk = "".join(f"pub fun bulk_{i}(a: usize, b: usize) usize {{ ret a + b + {i}; }}\n"
                           for i in range(20000))
            body = "use std.types.size.usize;\n" + bulk + text
            main.write_text(body, encoding="utf-8")

            session = LspSession(server, root, timeout)
            wedged = None
            try:
                session.request(
                    "initialize",
                    {"rootUri": root.as_uri(),
                     "capabilities": {"window": {"workDoneProgress": True}}},
                )
                session.notify("initialized", {})

                # resolved before the load starts, so stopping the worker is one
                # syscall rather than a process lookup inside the window
                for _ in range(200):
                    children = subprocess.run(["pgrep", "-P", str(session.proc.pid)],
                                              capture_output=True, text=True).stdout.split()
                    if children:
                        wedged = int(children[0])
                        break
                    time.sleep(0.02)
                require(wedged is not None, "no analysis worker to interrupt")

                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                      "version": 1, "text": body}},
                )
                begin = session.wait_for(
                    lambda item: (item.get("method") == "$/progress"
                                  and item["params"]["value"]["kind"] == "begin"),
                    "a progress report for the cold load")
                os.kill(wedged, signal.SIGSTOP)
                token = begin["params"]["token"]

                # a request the stopped worker cannot read, so the deadline ends it
                pending = session.next_id
                session.next_id += 1
                session._send({"jsonrpc": "2.0", "id": pending,
                               "method": "textDocument/documentSymbol",
                               "params": {"textDocument": {"uri": main.as_uri()}}})
                session.wait_for(lambda item: item.get("id") == pending,
                                 "an answer after the worker was ended")

                closed = session.wait_for(
                    lambda item: (item.get("method") == "$/progress"
                                  and item["params"].get("token") == token
                                  and item["params"]["value"]["kind"] == "end"),
                    "the abandoned progress report being closed")
                require(closed["params"]["value"].get("message"),
                        f"an abandoned report closed without saying why: {closed!r}")
            finally:
                with contextlib.suppress(Exception):
                    session.abort()
                if wedged is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(wedged, signal.SIGKILL)
    finally:
        if previous is None:
            os.environ.pop("MLS_REQUEST_DEADLINE_MS", None)
        else:
            os.environ["MLS_REQUEST_DEADLINE_MS"] = previous


def run_trace_policy(server: Path, timeout: float) -> None:
    """Turning tracing on must not copy the user's source into a log.

    A message body is the user's code: every `didOpen` carries a whole file and
    every `didChange` carries whatever they just typed. Tracing is usually
    turned on to find out which requests arrived in which order, and that
    question does not require any of it.

    So this greps the log rather than reading the code that writes it: the
    property is about what ends up on disk, and a test that inspected the call
    sites would keep passing if a new one were added.
    """
    marker = "SECRET_IDENTIFIER_NOT_FOR_THE_LOG"

    def session_writing(directory: Path, extra: dict[str, str]) -> str:
        log = directory / "trace.log"
        main, _, text = write_project(directory, "trace", 5)
        body = text + f"\npub fun {marker}(n: i32) i32 {{ ret n; }}\n"
        env = {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(log)}
        env.update(extra)
        session = LspSession(server, directory, timeout, env_extra=env)
        try:
            session.request("initialize", {"rootUri": directory.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": body}},
            )
            session.diagnostics(main.as_uri(), 1)
            session.request("textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            session.finish()
        finally:
            with contextlib.suppress(Exception):
                session.abort()
        require(log.exists(), "MLS_TRACE_FILE was ignored")
        return log.read_text(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory(prefix="mls-trace-off-") as directory:
        text = session_writing(Path(directory).resolve(), {})
        require(marker not in text,
                "tracing wrote the document's source to the log by default")
        require("method textDocument/documentSymbol" in text,
                f"tracing recorded no method metadata: {text[:400]!r}")
        require(re.search(r"(recv|send): \d+ bytes", text),
                f"tracing recorded no frame sizes: {text[:400]!r}")

    with tempfile.TemporaryDirectory(prefix="mls-trace-on-") as directory:
        text = session_writing(Path(directory).resolve(), {"MLS_TRACE": "bodies"})
        require('"method":"textDocument/documentSymbol"' in text,
                "the body opt-in recorded no bodies; log begins: "
                + repr(text[:400]))
        # and even then it is capped, because a trace that pages in a whole
        # buffer per keystroke is unreadable as well as invasive
        caps = re.findall(r"\.\.\. \[(\d+) of (\d+) bytes\]", text)
        require(caps, f"a body larger than the cap was written whole: {len(text)} bytes")
        for shown, total in caps:
            require(int(shown) < int(total), f"truncation marker is wrong: {shown}/{total}")


def run_exit_paths(server: Path, timeout: float) -> None:
    """Every lifecycle ending must terminate, with the documented code.

    `exit` means terminate, and a client may hold its end of the pipe open while
    it waits. With analysis on a worker the reading thread is parked in read(2),
    so an `exit` that only set a flag would leave the process alive until the
    client happened to close stdin.
    """
    cases = (
        (("shutdown", "exit"), False, 0),
        (("shutdown", "exit"), True, 0),
        (("exit",), False, 1),
        (("shutdown",), True, 0),
        ((), True, 1),
    )
    for steps, close_stdin, expected in cases:
        with tempfile.TemporaryDirectory(prefix="mls-exit-") as directory:
            root = Path(directory).resolve()
            session = LspSession(server, root, timeout)
            try:
                session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
                session.notify("initialized", {})
                if "shutdown" in steps:
                    response = session.request("shutdown")
                    require(response.get("result", object()) is None,
                            f"invalid shutdown response: {response!r}")
                if "exit" in steps:
                    session.notify("exit")
                if close_stdin:
                    session.proc.stdin.close()
                try:
                    code = session.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    session.proc.kill()
                    session.proc.wait()
                    raise ProtocolError(
                        f"server did not terminate for {steps!r} "
                        f"(stdin closed: {close_stdin})") from error
                require(code == expected,
                        f"{steps!r} (stdin closed: {close_stdin}) exited {code}, want {expected}")
            finally:
                if session.proc.poll() is None:
                    session.proc.kill()
                    session.proc.wait()


def run_clean_eof(server: Path, timeout: float) -> None:
    """A clean EOF after shutdown is not a malformed-frame failure."""
    with tempfile.TemporaryDirectory(prefix="mls-protocol-eof-") as directory:
        root = Path(directory).resolve()
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            exit_code, _, _ = session.finish(send_exit=False)
            require(exit_code == 0, f"clean EOF after shutdown exited {exit_code}")
            finished = True
        finally:
            if not finished:
                session.abort()


def run_transport_regressions(server: Path, timeout: float) -> None:
    """Exercise malformed/truncated and resource-bounded input framing."""
    cases = (
        (b"X-Header: value\r\n\r\n", "missing Content-Length"),
        (b"Content-Length: 4\r\n", "truncated header"),
        (b"Content-Length: 12junk\r\n\r\n", "malformed Content-Length"),
        (b"Content-Length: 4\r\nContent-Length: 4\r\n\r\nnull", "duplicate Content-Length"),
        (b"Content-Length: 20\r\n\r\n{}", "truncated body"),
        (f"Content-Length: {BODY_MAX + 1}\r\n\r\n".encode(), "oversized body"),
        (b"Content-Length: 999999999999999999999999999999999999\r\n\r\n", "overflowing length"),
        (b"X-Fill: " + (b"x" * HEADER_MAX), "oversized header"),
    )
    for frame, label in cases:
        run_bad_frame(server, frame, timeout, label)


def probe_closed_stdout(server: Path, timeout: float, restore_signals: bool) -> int:
    """Close the client read end, trigger output, and return the process status."""
    with tempfile.TemporaryDirectory(prefix="mls-protocol-pipe-") as directory:
        read_fd, write_fd = os.pipe()
        proc = subprocess.Popen(
            [str(server)],
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
            restore_signals=restore_signals,
        )
        os.close(write_fd)
        os.close(read_fd)
        assert proc.stdin is not None
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
        try:
            proc.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            proc.stdin.flush()
            proc.stdin.close()
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            proc.kill()
            proc.wait()
            raise ProtocolError("closed stdout reader: server did not terminate") from error


def main() -> int:
    """Run the suite and print request timing plus process-exit telemetry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", type=Path, help="path to the debug mls executable")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds allowed per response")
    args = parser.parse_args()
    server = args.server.resolve()
    if not server.is_file() or not os.access(server, os.X_OK):
        parser.error(f"server is not executable: {server}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        (exit_code, elapsed, message_count), timings = run_smoke(server, args.timeout)
        run_active_watcher_fallback(server, args.timeout)
        run_import_navigation(server, args.timeout)
        run_document_symbol_hierarchy(server, args.timeout)
        run_completion_context(server, args.timeout)
        run_document_highlight(server, args.timeout)
        run_workspace_symbol(server, args.timeout)
        run_signature_help(server, args.timeout)
        run_inlay_hints(server, args.timeout)
        run_semantic_tokens(server, args.timeout)
        run_cancellation(server, args.timeout)
        run_incremental_sync(server, args.timeout)
        run_code_actions(server, args.timeout)
        run_hover_presentation(server, args.timeout)
        run_same_fqn_reverse(server, args.timeout)
        run_clean_eof(server, args.timeout)
        run_exit_paths(server, args.timeout)
        run_crash_containment(server, args.timeout)
        run_hung_worker(server, args.timeout)
        run_progress_reporting(server, args.timeout)
        run_trace_policy(server, args.timeout)
        run_transport_regressions(server, args.timeout)
        closed_stdout_status = probe_closed_stdout(server, args.timeout, True)
        suppressed_status = probe_closed_stdout(server, args.timeout, False)
        require(suppressed_status == 1, f"suppressed SIGPIPE: expected exit 1, got {suppressed_status}")
        require(closed_stdout_status == 1, f"closed stdout reader: expected exit 1, got {closed_stdout_status}")
    except Exception as error:
        print(f"protocol smoke: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"protocol smoke: PASS ({message_count} messages, exit {exit_code}, {elapsed:.3f}s)")
    print("  use / fwd import paths navigate to their declarations")
    print("  documentSymbol nests members, and reflects edits through its cached parse")
    print("  completion answers for the cursor: members, exports, prefixes")
    print("  documentHighlight classifies reads and writes in the active file")
    print("  workspace/symbol searches loaded roots, best matches first")
    print("  signatureHelp tracks the active argument through incomplete calls")
    print("  inlayHint names literal arguments at multi-parameter calls")
    print("  semanticTokens decode in order, within the legend and the file")
    print("  a withdrawn request is answered RequestCancelled")
    print("  incremental sync patches ranges, ordered, in UTF-16 columns")
    print("  codeAction offers the compiler's own fixes as applicable edits")
    print("  hover renders headers, expression types, and the client's format")
    print("  clean EOF after shutdown: exit 0")
    print("  all five lifecycle endings terminate with the documented code")
    print("  a worker crash is answered, explained, and replayed into a replacement")
    print("  a wedged worker is ended, answered ServerCancelled, and recovered")
    print("  a cold load reports progress once, and an abandoned report is closed")
    print("  tracing keeps source out of the log unless asked for, and caps it")
    print("  malformed/oversized frames: 8 rejected with exit 1")
    print("  closed stdout reader with inherited SIG_IGN: exit 1")
    print("  closed stdout reader: exit 1")
    for label, duration in timings:
        print(f"  {label}: {duration:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
