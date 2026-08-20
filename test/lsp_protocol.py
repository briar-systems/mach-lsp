#!/usr/bin/env python3
"""Minimal live-stdio protocol smoke test for mach-lsp."""

from __future__ import annotations

import argparse
import json
import os
import queue
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

    def __init__(self, server: Path, cwd: Path, timeout: float) -> None:
        env = os.environ.copy()
        env.pop("MLS_TRACE", None)
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
            require(capabilities.get("textDocumentSync") == 1, "full-text sync is not advertised")
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
            broken = session.request(
                "textDocument/definition",
                {"textDocument": {"uri": alpha[0].as_uri()},
                 "position": {"line": 3, "character": 9}},
            )
            require(broken.get("result") is None,
                    f"watcher rejection disabled manifest fallback: {broken!r}")
            alpha_manifest.write_text(manifest_text, encoding="utf-8")
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
        run_same_fqn_reverse(server, args.timeout)
        run_clean_eof(server, args.timeout)
        run_exit_paths(server, args.timeout)
        run_transport_regressions(server, args.timeout)
        closed_stdout_status = probe_closed_stdout(server, args.timeout, True)
        suppressed_status = probe_closed_stdout(server, args.timeout, False)
        require(suppressed_status == 1, f"suppressed SIGPIPE: expected exit 1, got {suppressed_status}")
        require(closed_stdout_status == 1, f"closed stdout reader: expected exit 1, got {closed_stdout_status}")
    except Exception as error:
        print(f"protocol smoke: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"protocol smoke: PASS ({message_count} messages, exit {exit_code}, {elapsed:.3f}s)")
    print("  clean EOF after shutdown: exit 0")
    print("  all five lifecycle endings terminate with the documented code")
    print("  malformed/oversized frames: 8 rejected with exit 1")
    print("  closed stdout reader with inherited SIG_IGN: exit 1")
    print("  closed stdout reader: exit 1")
    for label, duration in timings:
        print(f"  {label}: {duration:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
