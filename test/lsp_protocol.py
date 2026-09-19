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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, NamedTuple

try:
    import fcntl
except ImportError:  # windows has no flock, so the rebuild-gate tests skip there
    fcntl = None

HEADER_MAX = 8 * 1024
BODY_MAX = 16 * 1024 * 1024
ANY_VERSION = object()


class ProtocolError(RuntimeError):
    """Raised when the live protocol session violates an asserted contract."""


def uri_file(uri: str) -> str:
    """The file a `file://` URI names, spelled so two URIs for it compare equal.

    The server percent-encodes what a client may leave bare, such as a Windows
    drive's colon, so URIs it builds are compared as the paths they name.
    """
    path = urllib.request.url2pathname(urllib.parse.unquote(urllib.parse.urlparse(uri).path))
    return os.path.normcase(os.path.normpath(path))


def manifest_of(uri: str) -> str | None:
    """The `mach.toml` of the nearest project enclosing a document, as `uri_file` spells it."""
    directory = Path(uri_file(uri)).parent
    for candidate in (directory, *directory.parents):
        if (candidate / "mach.toml").is_file():
            return uri_file((candidate / "mach.toml").as_uri())
    return None


class LspSession:
    """Drive one language-server process using LSP stdio framing."""

    def __init__(self, server: Path, cwd: Path, timeout: float,
                 env_extra: dict[str, str] | None = None, answer_progress: bool = False) -> None:
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
        # the manifests of the projects this session opened documents in. a load
        # publishes what it says about the project on exactly these (#266)
        self.manifests: set[str] = set()
        # answer the server's progress token requests as they arrive, as an
        # editor does, so the client's own responses are on the wire too
        self.answer_progress = answer_progress
        self.send_lock = threading.Lock()
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
                if self.answer_progress and message.get("method") == "window/workDoneProgress/create":
                    self.respond_result(message)
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
        if message.get("method") == "textDocument/didOpen":
            manifest = manifest_of(message["params"]["textDocument"]["uri"])
            if manifest is not None:
                self.manifests.add(manifest)
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        try:
            with self.send_lock:
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
        started = time.perf_counter()
        self._send(message)
        response = self.wait_for(lambda item: item.get("id") == request_id, f"response to {method}")
        self.timings.append((f"{method}#{request_id}", time.perf_counter() - started))
        if "error" in response:
            raise ProtocolError(f"{method} returned {response['error']!r}")
        return response

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its response, error or not."""
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self.wait_for(lambda item: item.get("id") == request_id, f"response to {method}")

    def request_after_notifications(
        self,
        notifications: list[tuple[str, dict[str, Any]]],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a queued notification burst followed by one request."""
        request_id = self.next_id
        self.next_id += 1
        messages = [
            {"jsonrpc": "2.0", "method": name, "params": notification_params}
            for name, notification_params in notifications
        ]
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        messages.append(request)

        frames = []
        for message in messages:
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
            frames.append(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        started = time.perf_counter()
        try:
            with self.send_lock:
                self.proc.stdin.write(b"".join(frames))
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ProtocolError(f"server stdin closed; stderr: {self.stderr_text()}") from error

        response = self.wait_for(lambda item: item.get("id") == request_id, f"response to {method}")
        self.timings.append((f"{method}#{request_id}", time.perf_counter() - started))
        if "error" in response:
            raise ProtocolError(f"{method} returned {response['error']!r}")
        return response

    def send_all(self, messages: list[dict[str, Any]]) -> None:
        """Write several complete messages in one write, so they queue together."""
        frames = []
        for message in messages:
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
            frames.append(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        try:
            with self.send_lock:
                self.proc.stdin.write(b"".join(frames))
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ProtocolError(f"server stdin closed; stderr: {self.stderr_text()}") from error

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

    def assert_no_message(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
        settle: float = 0.3,
    ) -> None:
        """Require that no matching message arrives during a short settle period."""
        for message in self.pending:
            if predicate(message):
                raise ProtocolError(f"unexpected {description}: {message!r}")
        deadline = time.monotonic() + settle
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty:
                return
            if item is None:
                raise ProtocolError(f"server exited while waiting for {description}; stderr: {self.stderr_text()}")
            if isinstance(item, BaseException):
                raise ProtocolError(f"response reader failed: {item}") from item
            assert isinstance(item, dict)
            self.message_count += 1
            if predicate(item):
                raise ProtocolError(f"unexpected {description}: {item!r}")
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

    def source_diagnostics(self, item: dict[str, Any]) -> bool:
        """Whether a message publishes diagnostics for anything but an opened project's manifest.

        A load inside a request publishes what it says about the project on that
        project's `mach.toml` (#266). Only those exact files are set aside: any
        other publish during a request is still one too many.
        """
        if item.get("method") != "textDocument/publishDiagnostics":
            return False
        uri = (item.get("params") or {}).get("uri", "")
        return uri_file(uri) not in self.manifests

    def quiet_diagnostics(self, settle: float = 0.4) -> list[dict[str, Any]]:
        """Return any diagnostics published since the last wait.

        Diagnostics belong to document state, so a feature request must not
        produce one. Anything a request republished is already in `pending`
        (the request's own reply drained the inbox past it); `settle` also
        catches a publish still in flight behind that reply.
        """
        found = [m for m in self.pending if self.source_diagnostics(m)]
        self.pending = [m for m in self.pending if not self.source_diagnostics(m)]
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
            if self.source_diagnostics(item):
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


class RebuildGate:
    """A turnstile the worker's finished off-thread rebuilds pass through, so a
    test can hold the snapshot behind the buffer while it asserts the isolated
    answer instead of racing the scheduler.

    The worker takes a shared lock on this file after each rebuild and drops it,
    blocking only while this gate holds the file's exclusive lock. Held, no
    rebuild is published and the buffer stays ahead of the snapshot, so
    completion is answered from the isolated path (`isIncomplete`). Released,
    held and future rebuilds flow and the snapshot catches up. `hold` and
    `release` are idempotent, so a test toggles per phase. The path is handed to
    the server through the `MLS_TEST_REBUILD_GATE` environment variable, which
    the supervisor passes to the worker it spawns.

    POSIX only: this drives the worker's flock turnstile with an exclusive lock,
    and Windows Python has no flock, so the tests using it skip there.
    """

    def __init__(self, directory: Path) -> None:
        self.path = (Path(directory) / "rebuild.gate").resolve()
        self.path.write_bytes(b"")
        self.fd = os.open(self.path, os.O_RDWR)
        self.held = False

    @property
    def env(self) -> dict[str, str]:
        return {"MLS_TEST_REBUILD_GATE": str(self.path)}

    def hold(self) -> None:
        """Keep finished rebuilds from being published until release()."""
        if not self.held:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.held = True

    def release(self) -> None:
        """Let held and future rebuilds through, so the snapshot catches up."""
        if self.held:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.held = False

    def close(self) -> None:
        self.release()
        os.close(self.fd)


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
vectorize = true
float_reassoc = false

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
                   f"use exports: {project_id}.bridge;\n"
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
        f"pub val own: i32 = {value};\nfwd {project_id}.defs.answer;\n", encoding="utf-8",
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
vectorize = true
float_reassoc = false

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
src = "src"
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


class BuildLog:
    """The server's own record of the project builds it ran, read from its trace.

    Every background build logs when it is scheduled and when it finishes, and
    the inline first load also logs that it was analyzed, so subtracting those
    leaves the background ones. Counting the server's record is deterministic,
    where waiting for the diagnostics stream to fall quiet is not: a build that
    is still running is quiet.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def env(self) -> dict[str, str]:
        return {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(self.path)}

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def scheduled(self) -> int:
        return self.text().count("off the analysis thread")

    def quiesced(self) -> bool:
        log = self.text()
        done = max(0, log.count("project: rebuilt") - log.count("project: analyzed"))
        return done >= log.count("off the analysis thread")

    def settle(self, at_least: int, description: str, deadline: float = 60.0) -> None:
        """Wait until at least `at_least` background builds ran and none is running."""
        eventually(lambda: self.scheduled() >= at_least and self.quiesced(),
                   lambda done: done, description, deadline)


def eventually(
    probe: Callable[[], Any],
    want: Callable[[Any], bool],
    description: str,
    deadline: float = 20.0,
) -> Any:
    """Poll a project-backed probe until the rebuild behind it has landed.

    A rebuild runs off the analysis thread, so the edit that scheduled it is
    answered from the PREVIOUS snapshot and the new one is swapped in a moment
    later. How long that moment is belongs to the machine, not the contract, so
    these are asserted with a deadline rather than a sleep.

    Waiting for the diagnostics stream to fall quiet cannot serve here: a
    rebuild that is still running IS quiet, so silence reads as settled on any
    machine slow enough for the question to matter.
    """
    end = time.monotonic() + deadline
    last: Any = None
    while True:
        last = probe()
        if want(last):
            return last
        if time.monotonic() >= end:
            raise ProtocolError(f"{description} never settled; last result {last!r}")
        time.sleep(0.05)


def settled_result(session: LspSession, method: str, params: dict[str, Any],
                   want: Callable[[Any], bool], description: str) -> Any:
    """Issue `method` until its result settles into `want`."""
    return eventually(lambda: session.request(method, params).get("result"),
                      want, description)


def assert_definition(session: LspSession, main: Path, definition: Path, text: str) -> None:
    """Check that `answer` resolves into the expected project root."""
    lines = text.splitlines()
    line = next(index for index, value in enumerate(lines) if " + direct + " in value)
    result = settled_result(
        session, "textDocument/definition",
        {"textDocument": {"uri": main.as_uri()},
         "position": {"line": line, "character": lines[line].index("direct") + 1}},
        lambda r: isinstance(r, dict), "definition of `direct`")
    require(isinstance(result, dict), f"definition is not a Location: {result!r}")
    require(result.get("uri") == definition.as_uri(), f"definition escaped its root: {result!r}")
    assert_range(result.get("range"), "definition.range")


def definition_after(session: LspSession, path: Path, text: str, prefix: str) -> dict[str, Any]:
    """Request definition of the member immediately following prefix."""
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if prefix in value and "use " not in value)
    character = lines[line].index(prefix) + len(prefix) + 1
    result = settled_result(
        session, "textDocument/definition",
        {"textDocument": {"uri": path.as_uri()},
         "position": {"line": line, "character": character}},
        lambda r: isinstance(r, dict), f"definition after {prefix!r}")
    require(isinstance(result, dict), f"definition after {prefix!r} is not a Location: {result!r}")
    return result


def definition(session: LspSession, path: Path, text: str, name: str) -> dict[str, Any]:
    """Request a definition at the last occurrence of name."""
    lines = text.splitlines()
    line = next(index for index in range(len(lines) - 1, -1, -1) if name in lines[index])
    result = settled_result(
        session, "textDocument/definition",
        {"textDocument": {"uri": path.as_uri()},
         "position": {"line": line, "character": lines[line].index(name) + 1}},
        lambda r: isinstance(r, dict), f"definition for {name}")
    require(isinstance(result, dict), f"definition for {name} is not a Location: {result!r}")
    return result


def run_smoke(server: Path, timeout: float) -> tuple[tuple[int, float, int], list[tuple[str, float]]]:
    """Run lifecycle, diagnostics, synchronization, and multi-root coverage."""
    with tempfile.TemporaryDirectory(prefix="mls-protocol-") as directory:
        root = Path(directory).resolve()
        alpha = write_project(root, "alpha", 11)
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
            session.assert_no_message(
                lambda item: item.get("id") == registration.get("id"),
                "reply to rejected watcher registration",
            )

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
            started = time.perf_counter()
            symbols = session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": alpha[0].as_uri()}},
            )
            require(time.perf_counter() - started < 1.0,
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
            # fallback active. A broken manifest is a FAILED REBUILD, not a
            # dropped snapshot: the failure is reported and the previous
            # snapshot keeps answering, so what is asserted is that the scan
            # fired and said so - not that cross-module features went dark.
            alpha_manifest = alpha[0].parents[1] / "mach.toml"
            manifest_text = alpha_manifest.read_text(encoding="utf-8")
            alpha_manifest.write_text(manifest_text + "\n[broken\n", encoding="utf-8")
            # the fingerprint fallback coalesces to at most one scan per 250 ms
            # per root, so a request issued inside that window never scans at
            # all. wait past it, or this asserts nothing.
            time.sleep(0.4)
            session.request(
                "textDocument/definition",
                {"textDocument": {"uri": alpha[0].as_uri()},
                 "position": {"line": 3, "character": 9}},
            )
            warning = session.wait_for(
                lambda item: (item.get("method") == "window/showMessage"
                              and isinstance(item.get("params"), dict)
                              and "failed to load project"
                              in str(item["params"].get("message", ""))),
                "broken manifest warning",
            )
            require(warning["params"].get("type") == 2,
                    f"load failure was not reported as a warning: {warning!r}")
            # the rebuild failed, so the snapshot it would have replaced is
            # still the one serving
            assert_definition(session, *alpha)
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
            broken_overlay = settled_result(
                session, "textDocument/definition",
                {"textDocument": {"uri": alpha_main.as_uri()},
                 "position": {"line": 8, "character": 30}},
                lambda r: r is None, "invalid unsaved import stops resolving")
            require(broken_overlay is None,
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
            after_close = settled_result(
                session, "textDocument/definition",
                {"textDocument": {"uri": alpha_main.as_uri()},
                 "position": {"line": 5, "character": 35}},
                lambda r: r is None, "closed unsaved export stops being authoritative")
            require(after_close is None,
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
            # routing, sema, and read-only rename are all exercised by the
            # unsaved i64 export.
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
            vendor_rename = session.call(
                "textDocument/rename",
                {"textDocument": {"uri": vendor_main.as_uri()},
                 "position": {"line": vendor_line, "character": vendor_char}, "newName": "changed"},
            )
            require((vendor_rename.get("error") or {}).get("code") == -32803,
                    f"vendored dependency rename was not refused: {vendor_rename!r}")
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


def write_wide_project(parent: Path, project_id: str, modules: int) -> tuple[Path, str]:
    """Create a project whose import closure is big enough to take real time.

    The rebuild has to outlast one request round-trip for the concurrency test
    to mean anything: against a three-file project, "answered during the
    rebuild" and "answered after it" are the same millisecond.
    """
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
vectorize = true
float_reassoc = false

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
    for index in range(modules):
        body = [f"pub val base{index}: i32 = {index};"]
        for k in range(24):
            body.append(f"pub fun f{index}_{k}(a: i32, b: i32) i32 {{ ret a * {k + 1} + b + base{index}; }}")
            body.append(f"pub rec R{index}_{k} {{ x: i32; y: i32; }}")
        (source / f"m{index}.mach").write_text("\n".join(body) + "\n", encoding="utf-8")
    uses = "\n".join(f"use {project_id}.m{index}.base{index};" for index in range(modules))
    total = " + ".join(f"base{index}" for index in range(modules))
    text = f"{uses}\n\npub fun main() i32 {{\n    ret {total};\n}}\n"
    main = source / "main.mach"
    main.write_text(text, encoding="utf-8")
    return main, text


def run_disk_change_during_build(server: Path, timeout: float) -> None:
    """A file written while a build runs is rebuilt, not mistaken for the snapshot's.

    A build reads a module early and records its disk fingerprint at the end. A
    write landing in between leaves a snapshot of the old bytes beside a
    fingerprint of the new ones, and a fingerprint scan then finds nothing to
    rebuild. Where in a build the write lands is not observable from outside, so
    it is tried across the build's duration; the window is most of the build,
    and every attempt must converge on what is on disk.
    """
    with tempfile.TemporaryDirectory(prefix="mls-midbuild-") as directory:
        root = Path(directory).resolve()
        main, text = write_wide_project(root, "mid", 384)
        module = main.parent / "m0.mach"
        module_text = module.read_text(encoding="utf-8")
        builds = BuildLog(root / "trace.log")
        session = LspSession(server, root, timeout, builds.env())
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
            line = next(i for i, value in enumerate(lines) if value.lstrip().startswith("ret "))
            position = {"line": line, "character": lines[line].index("base0") + 2}
            version = 1

            def rebuild() -> None:
                nonlocal version
                version += 1
                before = builds.scheduled()
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": text + f"\n# edit {version}\n"}]},
                )
                eventually(builds.scheduled, lambda n: n > before, "the rebuild to start")

            # the spare's first build is cold, so two rebuilds warm both sessions,
            # and the second says how long a warm one takes on this machine
            for _ in range(2):
                rebuild()
                builds.settle(0, "a timing rebuild")
            durations = re.findall(r"project: rebuilt .* in (\d+)ms", builds.text())
            warm = int(durations[-1]) / 1000.0

            for attempt, fraction in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)):
                value = 1000 + attempt
                rebuild()
                time.sleep(warm * fraction)
                module.write_text(module_text.replace("pub val base0: i32 = 0;",
                                                      f"pub val base0: i32 = {value};"),
                                  encoding="utf-8")
                builds.settle(0, "the interrupted rebuild")
                time.sleep(FINGERPRINT_WINDOW)
                settled_result(
                    session, "textDocument/hover",
                    {"textDocument": {"uri": main.as_uri()}, "position": position},
                    lambda r, want=value: f"base0: i32 = {want}" in json.dumps(r),
                    f"hover reflecting a write at {int(fraction * 100)}% of a rebuild")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()



STALE_MODULES = 192


def stale_fixture(root: Path, name: str, extra: str = "") -> tuple[Path, str, int]:
    """A project whose rebuild outlasts a request round-trip by a wide margin.

    `extra` is spliced into `main` just before its `ret`, and the returned line
    is the `ret` line's index in the opened text.
    """
    main, text = write_wide_project(root, name, STALE_MODULES)
    if extra:
        text = text.replace("    ret ", extra + "    ret ", 1)
        main.write_text(text, encoding="utf-8")
    ret_line = next(i for i, value in enumerate(text.splitlines()) if value.startswith("    ret "))
    return main, text, ret_line


def open_stale_session(server: Path, root: Path, timeout: float, main: Path, text: str,
                       capabilities: dict[str, Any] | None = None) -> tuple[LspSession, BuildLog]:
    builds = BuildLog(root / "trace.log")
    session = LspSession(server, root, timeout, builds.env())
    session.request("initialize", {"rootUri": root.as_uri(), "capabilities": capabilities or {}})
    session.notify("initialized", {})
    session.notify(
        "textDocument/didOpen",
        {"textDocument": {"uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}},
    )
    session.diagnostics(main.as_uri(), 1)
    return session, builds


def rebuilt(builds: BuildLog) -> int:
    return builds.text().count("project: rebuilt")


def change(uri: str, version: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": version},
                       "contentChanges": [{"text": text}]}}


def at(uri: str, line: int, character: int) -> dict[str, Any]:
    return {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}}


def stale_request(session: LspSession, builds: BuildLog, edit: dict[str, Any],
                  method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send an edit and a request in one write, and prove the answer was stale.

    Written together, the request is handled before the rebuild the edit
    started can finish, and the build log confirms it: no build completed
    between the edit and the answer.
    """
    before = rebuilt(builds)
    request_id = session.next_id
    session.next_id += 1
    session.send_all([edit, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}])
    response = session.wait_for(lambda item: item.get("id") == request_id, f"response to {method}")
    require(rebuilt(builds) == before,
            f"{method} was answered after the rebuild landed, so it proves nothing about staleness")
    return response


def run_stale_hover(server: Path, timeout: float) -> None:
    """A snapshot behind the buffer answers through the edit window (#251).

    Before the window an answer keeps its place, after it an answer moves with
    the text, and a cursor or a result touching the window answers nothing
    rather than a position the client no longer has.
    """
    with tempfile.TemporaryDirectory(prefix="mls-stale-hover-") as directory:
        root = Path(directory).resolve()
        main, text, ret_line = stale_fixture(root, "stale")
        uri = main.as_uri()
        session, builds = open_stale_session(server, root, timeout, main, text)
        finished = False
        try:
            column = text.splitlines()[ret_line].index("base3 ")

            def hover_lands(response: dict[str, Any], line: int, what: str) -> None:
                result = response.get("result")
                require(isinstance(result, dict), f"stale hover {what} answered nothing: {response!r}")
                require("base3" in json.dumps(result.get("contents")),
                        f"stale hover {what} described something else: {result!r}")
                start = result.get("range", {}).get("start")
                require(start == {"line": line, "character": column},
                        f"stale hover {what} is not where the client's text has it: {start!r}")

            # a line inserted above: the answer moves down with the text
            above = "# a line the snapshot never saw\n" + text
            before = rebuilt(builds)
            hover_lands(stale_request(session, builds, change(uri, 2, above),
                                      "textDocument/hover", at(uri, ret_line + 1, column + 2)),
                        ret_line + 1, "after the window")
            # the inserted line has no analysis, even where the same column of the
            # snapshot's first line holds a name
            inserted = session.request("textDocument/hover", at(uri, 0, len("use stale.m0.ba")))
            require(rebuilt(builds) == before, "the rebuild landed before the inserted line was asked about")
            require(inserted.get("result") is None,
                    f"a cursor on text the snapshot never saw answered: {inserted!r}")
            builds.settle(1, "the first edit's rebuild")

            # text appended below: the answer keeps its place
            below = above + "# and one after everything\n"
            hover_lands(stale_request(session, builds, change(uri, 3, below),
                                      "textDocument/hover", at(uri, ret_line + 1, column + 2)),
                        ret_line + 1, "before the window")
            builds.settle(2, "the second edit's rebuild")

            # the name itself replaced: the cursor still sits on bytes that exist
            # in both texts, but the name it resolves to overlaps the window
            renamed = below.replace("ret base0 + base1 + base2 + base3 ", "ret base0 + base1 + base2 + base9 ", 1)
            require(renamed != below, "the fixture's ret line changed shape")
            inside = stale_request(session, builds, change(uri, 4, renamed),
                                   "textDocument/hover", at(uri, ret_line + 1, column + 1))
            require(inside.get("result") is None,
                    f"a hover on a name the client has since edited answered: {inside!r}")
            builds.settle(3, "the third edit's rebuild")

            # and once the rebuild lands, the same position is answered afresh
            fresh = session.request("textDocument/hover", at(uri, ret_line + 1, column + 1))
            require("base9" in json.dumps(fresh.get("result")),
                    f"the rebuilt snapshot did not answer for the new name: {fresh!r}")

            # two edits far apart - a line at the top, a statement above `ret` -
            # leave the imports between them answerable, each moved by the first
            lines = renamed.splitlines(keepends=True)
            use_line = next(i for i, value in enumerate(lines) if value.startswith("use stale.m5.base5;"))
            spread = ("# top\n" + "".join(lines[:ret_line + 1])
                      + "    val late: i32 = base7;\n" + "".join(lines[ret_line + 1:]))
            between = stale_request(session, builds, change(uri, 5, spread),
                                    "textDocument/hover", at(uri, use_line + 1, len("use stale.m5.ba")))
            result = between.get("result")
            require(isinstance(result, dict) and "base5" in json.dumps(result.get("contents")),
                    f"a name between two edits answered nothing: {between!r}")
            require(result.get("range", {}).get("start", {}).get("line") == use_line + 1,
                    f"a name between two edits is not where the client has it: {result!r}")
            builds.settle(4, "the fourth edit's rebuild")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_stale_strict_requests(server: Path, timeout: float) -> None:
    """A rename or reference list waits for the snapshot that covers its edit.

    Answered from the stale snapshot, a rename would miss an occurrence the
    client just typed. Held, it is answered once the rebuild lands; withdrawing
    or closing while held is `run_stale_held_release`.
    """
    with tempfile.TemporaryDirectory(prefix="mls-stale-strict-") as directory:
        root = Path(directory).resolve()
        main, text, ret_line = stale_fixture(root, "strict")
        uri = main.as_uri()
        session, builds = open_stale_session(server, root, timeout, main, text)
        finished = False
        try:
            column = text.splitlines()[ret_line].index("base1 ")
            holds = lambda: builds.text().count("holding a request")

            # a new use of base1 is typed together with the rename request
            edited = text.replace("    ret base0 ", "    val again: i32 = base1;\n    ret base0 ", 1)
            request_id = session.next_id
            session.next_id += 1
            session.send_all([
                change(uri, 2, edited),
                {"jsonrpc": "2.0", "id": request_id, "method": "textDocument/rename",
                 "params": {**at(uri, ret_line + 1, column + 2), "newName": "renamed"}},
            ])
            response = session.wait_for(lambda item: item.get("id") == request_id, "held rename")
            require(holds() == 1, f"the rename was not held: {holds()} holds")
            edits = response.get("result", {}).get("changes", {}).get(uri, [])
            lines = sorted(e["range"]["start"]["line"] for e in edits)
            require(ret_line in lines and ret_line + 1 in lines,
                    f"the rename missed the occurrence typed with it: {lines!r}")
            builds.settle(1, "the rename's rebuild")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def held_behind_busy_root(server: Path, timeout: float, prefix: str,
                          release: Callable[[LspSession, str, int], None],
                          check: Callable[[dict[str, Any]], None]) -> None:
    """Hold a request for as long as the build slot is taken, then act on it.

    How long a rebuild takes belongs to the machine, so a request held only by
    its own root's rebuild can be answered before anything is done to it. Here
    the slot is first given to a much larger root's cold rebuild, and the held
    root cannot even start its own until that one finishes.
    """
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        root = Path(directory).resolve()
        held, held_text = write_wide_project(root, "heldroot", 64)
        busy, busy_text = write_wide_project(root, "busyroot", 768)
        builds = BuildLog(root / "trace.log")
        session = LspSession(server, root, timeout, builds.env())
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            for doc, text in ((busy, busy_text), (held, held_text)):
                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": doc.as_uri(), "languageId": "mach", "version": 1, "text": text}},
                )
                session.diagnostics(doc.as_uri(), 1)

            def rebuilds(name: str) -> int:
                return sum(1 for line in builds.text().splitlines()
                           if "project: rebuilt" in line and name in line)

            loaded = rebuilds("busyroot")
            session.send_all([change(busy.as_uri(), 2, busy_text + "# takes the build slot\n")])
            session.diagnostics(busy.as_uri(), 2)
            eventually(builds.scheduled, lambda n: n >= 1, "the busy root's rebuild to start")

            lines = held_text.splitlines()
            ret_line = next(i for i, value in enumerate(lines) if value.startswith("    ret "))
            request_id = session.next_id
            session.next_id += 1
            before = builds.text().count("holding a request")
            session.send_all([
                change(held.as_uri(), 2, held_text + "# held behind the busy root\n"),
                {"jsonrpc": "2.0", "id": request_id, "method": "textDocument/references",
                 "params": {**at(held.as_uri(), ret_line, lines[ret_line].index("base1") + 2),
                            "context": {"includeDeclaration": True}}},
            ])
            eventually(lambda: builds.text().count("holding a request"), lambda n: n > before,
                       "the request to be held")
            release(session, held.as_uri(), request_id)
            answer = session.wait_for(lambda item: item.get("id") == request_id, "the held request's answer")
            # answered while the busy root still holds the slot: nothing but the
            # release itself can have produced the answer
            require(rebuilds("busyroot") == loaded,
                    "the answer only came once the build slot freed up")
            check(answer)
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def cancel_request(session: LspSession, uri: str, request_id: int) -> None:
    session.notify("$/cancelRequest", {"id": request_id})


def close_document(session: LspSession, uri: str, request_id: int) -> None:
    session.notify("textDocument/didClose", {"textDocument": {"uri": uri}})


def run_stale_held_release(server: Path, timeout: float) -> None:
    """A held request is answered at once when withdrawn or when its document closes."""
    held_behind_busy_root(
        server, timeout, "mls-held-cancel-", cancel_request,
        lambda answer: require(answer.get("error", {}).get("code") == -32800,
                               f"a withdrawn held request was not cancelled: {answer!r}"))
    held_behind_busy_root(
        server, timeout, "mls-held-close-", close_document,
        lambda answer: require(answer.get("error", {}).get("code") == -32801,
                               f"a held request outlived its document: {answer!r}"))


def run_stale_refresh(server: Path, timeout: float) -> None:
    """Answers given from a stale snapshot are replaced once the rebuild lands.

    Semantic tokens and inlay hints are re-requested by a client only when told
    to, so the server asks - and only a client that said it can take the request.
    """
    for supported in (True, False):
        with tempfile.TemporaryDirectory(prefix="mls-stale-refresh-") as directory:
            root = Path(directory).resolve()
            main, text, _ = stale_fixture(root, "refresh")
            uri = main.as_uri()
            capabilities = {"workspace": {"semanticTokens": {"refreshSupport": supported},
                                          "inlayHint": {"refreshSupport": supported}}}
            session, builds = open_stale_session(server, root, timeout, main, text, capabilities)
            finished = False
            try:
                tokens = stale_request(session, builds, change(uri, 2, "# moved\n" + text),
                                       "textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})
                require(tokens.get("result", {}).get("data"),
                        f"stale semantic tokens answered nothing: {tokens!r}")
                builds.settle(1, "the rebuild behind the stale tokens")
                refreshes = lambda item: item.get("method") in (
                    "workspace/semanticTokens/refresh", "workspace/inlayHint/refresh")
                if supported:
                    for _ in range(2):
                        request = session.wait_for(refreshes, "a refresh request")
                        session.respond_result(request)
                else:
                    session.assert_no_message(refreshes, "refresh request to a client that cannot take one")
                session.finish()
                finished = True
            finally:
                if not finished:
                    session.abort()


def run_stale_diagnostics(server: Path, timeout: float) -> None:
    """While the root rebuilds, a buffer shows what can still be said about it.

    A semantic error away from the edit is carried to its new line; a buffer
    that no longer parses shows its syntax errors and nothing older.
    """
    with tempfile.TemporaryDirectory(prefix="mls-stale-diags-") as directory:
        root = Path(directory).resolve()
        main, text, _ = stale_fixture(root, "diags", "    val bad: i32 = nowhere;\n")
        uri = main.as_uri()
        bad_line = next(i for i, value in enumerate(text.splitlines()) if "nowhere" in value)
        builds = BuildLog(root / "trace.log")
        session = LspSession(server, root, timeout, builds.env())
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "mach", "version": 1, "text": text}},
            )
            opened = session.diagnostics(uri, 1)["params"]["diagnostics"]
            require(bad_line in [d["range"]["start"]["line"] for d in opened],
                    f"the fixture's unresolved name was not reported: {opened!r}")

            def publish(version: int, new_text: str) -> list[int]:
                before = rebuilt(builds)
                session.send_all([change(uri, version, new_text)])
                published = session.diagnostics(uri, version)
                require(rebuilt(builds) == before, "the publish came after the rebuild")
                return [d["range"]["start"]["line"] for d in published["params"]["diagnostics"]]

            moved = "# pushes everything down\n" + text
            lines = publish(2, moved)
            require(bad_line + 1 in lines,
                    f"the unresolved name was not carried to its new line: {lines!r}")
            builds.settle(1, "the first edit's rebuild")
            session.diagnostics(uri, 2)

            broken = moved + "pub fun half( {\n"
            lines = publish(3, broken)
            require(lines, "a buffer that no longer parses showed no errors")
            require(bad_line + 1 not in lines,
                    f"a stale semantic error was shown beside the syntax errors: {lines!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_version(server: Path, timeout: float) -> None:
    """The binary reports the version it was built from, and starts nothing to do so.

    `mls --version` names the server and the compiler it links; `initialize`
    reports them as separate keys, so `serverInfo.version` stays exactly the
    server's own. Both come from the manifests the binary was built from.
    """
    repo = Path(__file__).resolve().parents[1]

    def manifest_version(path: Path) -> str:
        match = re.search(r'^version = "([^"]+)"', path.read_text(encoding="utf-8"), re.M)
        require(match is not None, f"{path} has no project version")
        return match.group(1)

    expected = manifest_version(repo / "mach.toml")
    compiler = manifest_version(repo / "dep" / "mach" / "mach.toml")

    done = subprocess.run([str(server), "--version"], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)
    require(done.returncode == 0, f"--version exited {done.returncode}: {done.stderr!r}")
    require(done.stdout.decode().strip() == f"mls {expected} (mach {compiler})",
            f"--version printed {done.stdout!r}, expected mls {expected} (mach {compiler})")

    with tempfile.TemporaryDirectory(prefix="mls-version-") as directory:
        session = LspSession(server, Path(directory), timeout)
        finished = False
        try:
            info = session.request("initialize", {"capabilities": {}})["result"].get("serverInfo", {})
            require(info == {"name": "mach-lsp", "version": expected, "mach": compiler},
                    f"initialize reported {info!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_position_encoding(server: Path, timeout: float) -> None:
    """The server negotiates `positionEncoding` and honours it at the boundary.

    utf-16 is the base-protocol default and 1.0's promise, so a client that
    advertises nothing still gets it. Offered utf-8 (mach's own byte offsets)
    or utf-32, the server picks it and echoes it in ServerCapabilities. The
    choice then decides what a `character` column counts: the same reference,
    after a non-BMP codepoint where the encodings disagree, sits at a different
    column in each, and every one must still resolve to the same definition
    through the single conversion point in `positions`.
    """
    manifest = """[project]
id = "penc"
version = "0.1.0"
src = "src"
out = "out/{target.name}/{profile.name}"

[target.linux-x86_64]
isa = "x86_64"
os = "linux"
abi = "sysv64"

[profile.debug]
opt = 0
debug = true
simd = "scalarize"
vectorize = true
float_reassoc = false

[artifact.app]
kind = "bin"
entry = "main.mach"
out = "bin/app"
targets = ["*"]
link = []
need = []
"""
    glyph = "\U0001F600"  # U+1F600, a non-BMP codepoint: 2 utf-16 units, 4 utf-8 bytes, 1 utf-32
    source = (
        "pub fun target() i32 { ret 0; }\n"
        "\n"
        'pub fun main() i32 { val e: str = "' + glyph + '"; ret target(); }\n'
    )
    lines = source.splitlines()
    ref_line = next(i for i, v in enumerate(lines) if "ret target()" in v)
    idx = lines[ref_line].index("target(")  # codepoint index of the reference
    prefix = lines[ref_line][:idx]
    columns = {
        "utf-16": len(prefix.encode("utf-16-le")) // 2,
        "utf-8": len(prefix.encode("utf-8")),
        "utf-32": len(prefix),
    }
    # the three columns must genuinely differ, or the buffer proves nothing
    require(len(set(columns.values())) == 3,
            f"the non-BMP buffer did not separate the encodings: {columns!r}")

    def negotiated(offered: object) -> tuple[LspSession, Path, str]:
        directory = tempfile.mkdtemp(prefix="mls-penc-")
        root = Path(directory).resolve()
        src = root / "src"
        src.mkdir(parents=True)
        (root / "mach.toml").write_text(manifest, encoding="utf-8")
        main = src / "main.mach"
        main.write_text(source, encoding="utf-8")
        session = LspSession(server, root, timeout)
        general = {} if offered is None else {"positionEncodings": offered}
        answer = session.request(
            "initialize", {"rootUri": root.as_uri(), "capabilities": {"general": general}})
        echoed = answer["result"].get("capabilities", {}).get("positionEncoding")
        return session, main, echoed

    def resolves(session: LspSession, main: Path, character: int) -> None:
        session.notify("initialized", {})
        session.notify("textDocument/didOpen", {"textDocument": {
            "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": source}})
        session.diagnostics(main.as_uri(), 1)
        result = eventually(
            lambda: session.request("textDocument/definition", {
                "textDocument": {"uri": main.as_uri()},
                "position": {"line": ref_line, "character": character}}).get("result"),
            lambda r: isinstance(r, dict), "definition of `target`")
        require(isinstance(result, dict) and result.get("uri") == main.as_uri(),
                f"definition did not resolve to the source file: {result!r}")
        # the definition sits on the first line, not the reference line
        start = (result.get("range") or {}).get("start") or {}
        require(start.get("line") == 0,
                f"definition resolved to the wrong line: {result!r}")

    # a client that advertises nothing still gets exactly what 1.0 promised
    session, main, echoed = negotiated(None)
    finished = False
    try:
        require(echoed == "utf-16",
                f"no advertisement should default to utf-16, got {echoed!r}")
        resolves(session, main, columns["utf-16"] + 1)
        session.finish()
        finished = True
    finally:
        if not finished:
            session.abort()

    # what the client offers is honoured, and the column is read in those units
    for offered, want in (
        (["utf-8", "utf-16"], "utf-8"),      # utf-8 wins even when utf-16 is also offered
        (["utf-32"], "utf-32"),               # utf-32 alone
        (["utf-16"], "utf-16"),               # utf-16 alone
    ):
        session, main, echoed = negotiated(offered)
        finished = False
        try:
            require(echoed == want,
                    f"offered {offered!r}, server chose {echoed!r}, expected {want!r}")
            resolves(session, main, columns[want] + 1)
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_manifest_notes(server: Path, timeout: float) -> None:
    """What a load says about the project itself shows on its mach.toml (#266).

    A manifest without `[project].mach` loads with a warning that belongs to no
    source file, and a compiler outside a stated range refuses the load. Both are
    published on the root's `mach.toml`, and cleared when the manifest is fixed:
    through `workspace/didChangeWatchedFiles`, through the next request when the
    client sends no such notification, and for a root that never loaded as well
    as one that did. A stale complaint on a fixed file is the failure this
    guards.
    """
    # the range the warning tells the user to add is read from the warning itself,
    # since the compiler chooses it (the oldest release that reads the key, not the
    # running one) and may change it across versions
    admitted = ""
    refused = 'mach = "^99"'

    def notes_for(session: LspSession, uri: str, want: Callable[[list[dict[str, Any]]], bool],
                  what: str) -> list[dict[str, Any]]:
        message = session.wait_for(
            lambda item: (item.get("method") == "textDocument/publishDiagnostics"
                          and uri_file(item.get("params", {}).get("uri", "")) == uri_file(uri)
                          and want(item["params"].get("diagnostics", []))),
            what)
        return message["params"]["diagnostics"]

    def one(severity: int, text: str) -> Callable[[list[dict[str, Any]]], bool]:
        return lambda found: (len(found) == 1 and found[0].get("severity") == severity
                              and text in found[0].get("message", ""))

    def cleared(found: list[dict[str, Any]]) -> bool:
        return found == []

    def changed(session: LspSession, uri: str) -> None:
        session.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 2}]})

    def open_doc(session: LspSession, root: Path, main: Path, text: str) -> None:
        session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
        session.notify("initialized", {})
        session.notify("textDocument/didOpen", {"textDocument": {
            "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})

    with tempfile.TemporaryDirectory(prefix="mls-notes-") as directory:
        main, _, text = write_project(Path(directory).resolve(), "notes", 3)
        root = Path(directory).resolve() / "notes"
        manifest = root / "mach.toml"
        uri = manifest.as_uri()
        base = manifest.read_text(encoding="utf-8")
        require(base.startswith("[project]\n") and "mach =" not in base,
                "write_project's manifest changed shape")

        def set_range(line: str | None) -> None:
            manifest.write_text(base if line is None else base.replace("[project]\n", f"[project]\n{line}\n", 1),
                                encoding="utf-8")

        # a loaded root
        session = LspSession(server, root, timeout)
        finished = False
        try:
            open_doc(session, root, main, text)
            found = notes_for(session, uri, one(2, "states no compiler range"),
                              "the missing-range warning on mach.toml")
            suggested = re.search(r'mach = "([^"]+)"', found[0]["message"])
            require(suggested is not None,
                    f"the warning does not name the line to add: {found[0]!r}")
            admitted = f'mach = "{suggested.group(1)}"'
            session.diagnostics(main.as_uri(), 1)

            set_range(admitted)
            changed(session, uri)
            notes_for(session, uri, cleared, "the warning cleared once the range was added")

            set_range(refused)
            changed(session, uri)
            notes_for(session, uri, one(1, "does not accept it"),
                      "the refusal on mach.toml")

            # no watcher notification: the next request finds the new manifest
            time.sleep(0.4)
            set_range(admitted)
            time.sleep(0.4)
            # a semantic request is what consults the project, and with it the disk
            session.request("textDocument/hover", {"textDocument": {"uri": main.as_uri()},
                                                   "position": {"line": 0, "character": 0}})
            notes_for(session, uri, cleared, "the refusal cleared by a request after an unannounced fix")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()

        # a root that never loaded
        set_range(refused)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            open_doc(session, root, main, text)
            notes_for(session, uri, one(1, "does not accept it"),
                      "the refusal of a first load on mach.toml")
            session.wait_for(lambda item: (item.get("method") == "window/showMessage"
                                           and "failed to load project" in item["params"].get("message", "")),
                             "the load failure message")

            set_range(admitted)
            changed(session, uri)
            notes_for(session, uri, cleared, "the refusal cleared once the root loaded")
            symbols = session.request("textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            require(symbols.get("result"), f"the fixed root does not answer: {symbols!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_settings(server: Path, timeout: float) -> None:
    """What a client configures at `initialize`, and how it combines (#264).

    The option wins over the environment, the environment over the LSP
    `initialize.trace`, and `$/setTrace` moves the level afterwards. A key the
    server does not know, or a value it cannot use, from an option or the
    environment, is noted and ignored; it never fails `initialize`. Nothing is
    traced before `initialize` has decided where the trace goes (#285).
    """
    SAMPLE = "pub fun sample(n: i32) i32 {\n    ret n;\n}\n"

    def session_with(directory: Path, params: dict[str, Any], env: dict[str, str]) -> tuple[LspSession, Path]:
        doc = directory / "sample.mach"
        doc.write_text(SAMPLE, encoding="utf-8")
        session = LspSession(server, directory, timeout, env)
        answer = session.request("initialize", {"capabilities": {}, **params})
        require("result" in answer, f"initialize failed under {params!r}: {answer!r}")
        session.notify("initialized", {})
        session.notify("textDocument/didOpen", {"textDocument": {
            "uri": doc.as_uri(), "languageId": "mach", "version": 1, "text": SAMPLE}})
        session.diagnostics(doc.as_uri(), 1)
        return session, doc

    def symbols(session: LspSession, doc: Path) -> None:
        session.request("textDocument/documentSymbol", {"textDocument": {"uri": doc.as_uri()}})

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def run(params: dict[str, Any], env: dict[str, str], body: Callable[[LspSession, Path, Path], None]) -> None:
        with tempfile.TemporaryDirectory(prefix="mls-settings-") as name:
            directory = Path(name).resolve()
            session, doc = session_with(directory, params, env)
            finished = False
            try:
                body(session, doc, directory)
                session.finish()
                finished = True
            finally:
                if not finished:
                    session.abort()

    # the options alone turn tracing on, choose the file, and note what they ignore
    def options(session: LspSession, doc: Path, directory: Path) -> None:
        log = directory / "chosen.log"
        symbols(session, doc)
        text = read(log)
        require("method textDocument/documentSymbol" in text,
                f"the trace option did not trace into traceFile: {text[:400]!r}")
        require('"method":"textDocument/documentSymbol"' not in text,
                "messages level wrote a message body")
        require("unknown option `colour` ignored" in text, f"an unknown key was not noted: {text[:600]!r}")
        require("requestDeadlineMs` 10 is below 1000" in text, f"a short deadline was not noted: {text[:600]!r}")

        # $/setTrace moves the level either way
        session.notify("$/setTrace", {"value": "verbose"})
        symbols(session, doc)
        require('"method":"textDocument/documentSymbol"' in read(log), "verbose did not add message bodies")
        session.notify("$/setTrace", {"value": "off"})
        quiet = read(log).count("method textDocument/documentSymbol")
        symbols(session, doc)
        require(read(log).count("method textDocument/documentSymbol") == quiet,
                "$/setTrace off still traced the next request")

    with tempfile.TemporaryDirectory(prefix="mls-settings-log-") as logdir:
        chosen = Path(logdir).resolve() / "chosen.log"
        run({"initializationOptions": {"trace": "messages", "traceFile": str(chosen),
                                       "colour": "blue", "requestDeadlineMs": 10}},
            {}, lambda s, d, _: options(s, d, chosen.parent))

    # a relative traceFile is under the workspace root: the first folder, else rootUri
    def rooted(session: LspSession, doc: Path, directory: Path) -> None:
        symbols(session, doc)
        text = read(directory / "relative.log")
        require("method textDocument/documentSymbol" in text,
                f"a relative traceFile was not resolved against the workspace root: {sorted(directory.iterdir())!r}")
        require("to " + str(directory / "relative.log") + " (option)" in text,
                f"the resolved traceFile was not traced: {text[:600]!r}")

    for params in (
        lambda root: {"rootUri": root.as_uri()},
        lambda root: {"rootUri": "file:///nonexistent-root",
                      "workspaceFolders": [{"uri": root.as_uri(), "name": "w"}]},
    ):
        with tempfile.TemporaryDirectory(prefix="mls-settings-root-") as rootdir:
            root = Path(rootdir).resolve()
            run({**params(root), "initializationOptions": {"trace": "messages", "traceFile": "logs/../relative.log"}},
                {}, lambda s, d, _: rooted(s, d, root))

    # with no root, it falls back to the environment's, with a note there
    def unrooted(session: LspSession, doc: Path, directory: Path) -> None:
        symbols(session, doc)
        text = read(directory / "env.log")
        require("method textDocument/documentSymbol" in text, "a relative traceFile did not fall back")
        require("is relative and there is no workspace root" in text,
                f"the unrooted traceFile was not noted: {text[:600]!r}")

    with tempfile.TemporaryDirectory(prefix="mls-settings-env-") as logdir:
        envlog = Path(logdir).resolve() / "env.log"
        run({"initializationOptions": {"trace": "messages", "traceFile": "relative.log"}},
            {"MLS_TRACE_FILE": str(envlog)}, lambda s, d, _: unrooted(s, d, envlog.parent))

        # a relative path means a path under the root: one that climbs out is
        # refused the same way, and nothing is written beside the root (#300)
        def escaping(session: LspSession, doc: Path, root: Path) -> None:
            symbols(session, doc)
            text = read(envlog)
            require("method textDocument/documentSymbol" in text, "an escaping traceFile did not fall back")
            require("resolves outside the workspace root" in text,
                    f"the escaping traceFile was not noted: {text[:600]!r}")
            require(not (root.parent / "escape.log").exists(), "an escaping traceFile was written beside the root")

        envlog.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="mls-settings-escape-") as outer:
            root = Path(outer).resolve() / "root"
            root.mkdir()
            run({"rootUri": root.as_uri(),
                 "initializationOptions": {"trace": "messages", "traceFile": "../escape.log"}},
                {"MLS_TRACE_FILE": str(envlog)}, lambda s, d, _: escaping(s, d, root))

        # so does one too long to open
        envlog.unlink(missing_ok=True)
        run({"initializationOptions": {"trace": "messages", "traceFile": "/" + "x" * 600}},
            {"MLS_TRACE_FILE": str(envlog)},
            lambda s, d, _: (symbols(s, d), require("is longer than 511 bytes" in read(envlog),
                                                    f"a long traceFile was not noted: {read(envlog)[:400]!r}")))

        # the LSP trace value turns tracing on when the environment does not
        envlog.unlink(missing_ok=True)
        run({"trace": "messages"}, {"MLS_TRACE_FILE": str(envlog)},
            lambda s, d, _: (symbols(s, d), require("method textDocument/documentSymbol" in read(envlog),
                                                    "initialize.trace messages did not trace")))

        # but an editor's default `trace: "off"` does not silence the environment
        envlog.unlink(missing_ok=True)
        run({"trace": "off"}, {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(envlog)},
            lambda s, d, _: (symbols(s, d), require("method textDocument/documentSymbol" in read(envlog),
                                                    "initialize.trace off silenced MLS_TRACE")))

        # while the option does, entirely: nothing from before `initialize`
        # reaches the environment's file either, bodies included (#285)
        envlog.unlink(missing_ok=True)
        run({"initializationOptions": {"trace": "off"}}, {"MLS_TRACE": "bodies", "MLS_TRACE_FILE": str(envlog)},
            lambda s, d, _: (symbols(s, d), require(read(envlog) == "",
                                                    f"the trace option did not silence MLS_TRACE: {read(envlog)[:400]!r}")))

        # a chosen file takes the whole session, its start included, and the
        # environment's file is never written
        envlog.unlink(missing_ok=True)
        chosen = envlog.parent / "chosen.log"
        run({"initializationOptions": {"traceFile": str(chosen)}},
            {"MLS_TRACE": "bodies", "MLS_TRACE_FILE": str(envlog)},
            lambda s, d, _: symbols(s, d))
        require(read(envlog) == "", f"a chosen traceFile left lines in MLS_TRACE_FILE: {read(envlog)[:400]!r}")
        text = read(chosen)
        require("=== mach-lsp started ===" in text and "method initialize" in text,
                f"the lines from before initialize did not reach the chosen file: {text[:400]!r}")
        require(re.search(r"recv: \d+ bytes\n", text) and '"method":"initialize"' not in text,
                f"a line from before initialize carried a body: {text[:600]!r}")
        require('"method":"textDocument/documentSymbol"' in text, "MLS_TRACE=bodies stopped applying after initialize")
        require(f"settings: trace bodies (environment), to {chosen} (option), request deadline 120000ms (default)" in text,
                f"the effective settings were not traced: {text[:800]!r}")

        # MLS_TRACE=off is off, and unusable environment values are noted
        envlog.unlink(missing_ok=True)
        run({}, {"MLS_TRACE": "off", "MLS_TRACE_FILE": str(envlog)}, lambda s, d, _: symbols(s, d))
        require(read(envlog) == "", f"MLS_TRACE=off traced: {read(envlog)[:400]!r}")
        for value, note in (("soon", "MLS_REQUEST_DEADLINE_MS `soon` is not an integer; ignored"),
                            ("10", "MLS_REQUEST_DEADLINE_MS 10 is below 1000; ignored")):
            envlog.unlink(missing_ok=True)
            run({}, {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(envlog), "MLS_REQUEST_DEADLINE_MS": value},
                lambda s, d, _: symbols(s, d))
            require(note in read(envlog), f"an unusable environment deadline was not noted: {read(envlog)[:600]!r}")
        envlog.unlink(missing_ok=True)
        run({}, {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(envlog), "MLS_REQUEST_DEADLINE_MS": "4000"},
            lambda s, d, _: symbols(s, d))
        require("request deadline 4000ms (environment)" in read(envlog),
                f"the environment's deadline was not traced: {read(envlog)[:600]!r}")

    # with no file named, the trace goes to stderr, where an editor collects it
    def to_stderr(session: LspSession, doc: Path, directory: Path) -> None:
        symbols(session, doc)
        session.finish()
        session.stderr_reader.join(timeout=timeout)
        err = session.stderr_text()
        require("method textDocument/documentSymbol" in err, f"the trace did not go to stderr: {err[:400]!r}")
        require("to stderr (default)" in err, f"stderr was not named as the destination: {err[:600]!r}")

    with tempfile.TemporaryDirectory(prefix="mls-settings-stderr-") as name:
        directory = Path(name).resolve()
        session, doc = session_with(directory, {}, {"MLS_TRACE": "1"})
        try:
            to_stderr(session, doc, directory)
        finally:
            with contextlib.suppress(Exception):
                session.abort()

    # a session that ends before `initialize` still writes what it kept
    with tempfile.TemporaryDirectory(prefix="mls-settings-early-") as name:
        directory = Path(name).resolve()
        early = directory / "early.log"
        session = LspSession(server, directory, timeout, {"MLS_TRACE": "1", "MLS_TRACE_FILE": str(early)})
        try:
            session.proc.stdin.close()
            code = session.proc.wait(timeout=timeout)
            require(code == 1, f"input closing before initialize exited {code}, want 1")
        finally:
            with contextlib.suppress(Exception):
                session.abort()
        require("=== mach-lsp started ===" in read(early) and "input closed" in read(early),
                f"a session with no initialize lost its trace: {read(early)[:400]!r}")


def worker_pid(session: "LspSession") -> int:
    """The analysis worker's pid: the supervisor's only child."""
    for _ in range(200):
        children = subprocess.run(["pgrep", "-P", str(session.proc.pid)],
                                  capture_output=True, text=True).stdout.split()
        if children:
            return int(children[0])
        time.sleep(0.02)
    raise AssertionError("no analysis worker")


class SlowProject(NamedTuple):
    main: Path
    text: str


def slow_project(parent: Path, project_id: str, value: int) -> SlowProject:
    """A project whose load, and whose first rebuild, last long enough to stop
    the worker inside them. The bulk is in its own module, so the entry module,
    and every message about it, stays small."""
    main, _, text = write_project(parent, project_id, value)
    bulk = main.parent / "bulk.mach"
    bulk_text = "".join(f"pub fun bulk_{i}(a: i32, b: i32) i32 {{ ret a + b + {i}; }}\n"
                        for i in range(20000))
    bulk.write_text(bulk_text, encoding="utf-8")
    text = f"use {project_id}.bulk;\n" + text
    main.write_text(text, encoding="utf-8")
    return SlowProject(main, text)


def run_deadline_spares_load(server: Path, timeout: float) -> None:
    """`requestDeadlineMs` bounds a request being handled, never a project load (#284).

    A load is the worker doing the work a request waits on. Holding the load to
    the request deadline ended every worker a deadline shorter than the load
    reached, and the replacement reloaded, so it was ended too, until the server
    gave up. The client here behaves as an editor does: it advertises progress
    and answers the server's token requests, which are responses on the client
    stream and must never be taken for requests.

    The worker is stopped inside its cold load for well past the deadline, then
    resumed: the request waiting on the load is answered with a result, by the
    same worker. A rename held for a rebuild is answered the same way after the
    worker is stopped past the deadline while holding it. A request that really
    wedges is still ended, and the replacement, which loads again under the same
    deadline, serves.
    """
    if os.name != "posix":
        print("  deadline spares load: skipped (needs pgrep and SIGSTOP)")
        return
    with tempfile.TemporaryDirectory(prefix="mls-deadload-") as directory:
        root = Path(directory).resolve()
        slow = slow_project(root, "deadload", 3)
        main, body = slow.main, slow.text
        trace_log = root / "trace.log"
        session = LspSession(server, root, timeout, answer_progress=True)
        stopped = None
        finished = False
        try:
            session.request("initialize", {
                "rootUri": root.as_uri(),
                "capabilities": {"window": {"workDoneProgress": True}},
                "initializationOptions": {"requestDeadlineMs": 1000, "trace": "messages",
                                          "traceFile": str(trace_log)}})
            session.notify("initialized", {})
            worker = worker_pid(session)
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": body}})
            begin = session.wait_for(
                lambda item: (item.get("method") == "$/progress"
                              and item["params"]["value"]["kind"] == "begin"),
                "the cold load's progress report")
            os.kill(worker, signal.SIGSTOP)
            stopped = worker
            token = begin["params"]["token"]

            line = next(i for i, l in enumerate(body.split("\n")) if "ret take[i32](b)" in l)
            column = body.split("\n")[line].index("take") + 1
            waiting = session.next_id
            session.next_id += 1
            session._send({"jsonrpc": "2.0", "id": waiting, "method": "textDocument/hover",
                           "params": {"textDocument": {"uri": main.as_uri()},
                                      "position": {"line": line, "character": column}}})
            session.assert_no_message(
                lambda item: (item.get("id") == waiting
                              or item.get("method") == "window/showMessage"
                              or (item.get("method") == "$/progress"
                                  and item["params"]["value"]["kind"] == "end")),
                "an answer, a crash report, or the load ending while the worker is stopped",
                settle=2.5)
            os.kill(worker, signal.SIGCONT)
            stopped = None

            # the load's end restarts the waiting request's clock: stopped again
            # for less than the deadline, it is still not ended, although it
            # arrived long before
            session.wait_for(lambda item: (item.get("method") == "$/progress"
                                           and item["params"].get("token") == token
                                           and item["params"]["value"]["kind"] == "end"),
                             "the load's own progress end")
            os.kill(worker, signal.SIGSTOP)
            stopped = worker
            time.sleep(0.6)
            os.kill(worker, signal.SIGCONT)
            stopped = None
            answer = session.wait_for(lambda item: item.get("id") == waiting, "the hover that waited on the load")
            require("result" in answer and answer["result"],
                    f"a request waiting on the load was not answered by it: {answer!r}")
            require(worker_pid(session) == worker, "the worker was replaced during its load")

            # a rename held for the rebuild an edit starts waits past the deadline
            # too, and is answered from the rebuilt snapshot. how long a rebuild
            # takes depends on the machine, so the worker is stopped once it
            # reports the hold, for longer than the deadline
            holding = "server: holding a request until its snapshot catches up"
            holds = trace_log.read_text(encoding="utf-8", errors="replace").count(holding)
            session.notify("textDocument/didChange", {
                "textDocument": {"uri": main.as_uri(), "version": 2},
                "contentChanges": [{"text": body + "\n# edited\n"}]})
            renaming = session.next_id
            session.next_id += 1
            session._send({"jsonrpc": "2.0", "id": renaming, "method": "textDocument/rename",
                           "params": {"textDocument": {"uri": main.as_uri()},
                                      "position": {"line": line, "character": column}, "newName": "take_all"}})
            deadline = time.monotonic() + timeout
            while trace_log.read_text(encoding="utf-8", errors="replace").count(holding) == holds:
                require(time.monotonic() < deadline, "the rename was never held")
                session.assert_no_message(lambda item: item.get("id") == renaming,
                                          "rename answer before it was held, which proves nothing",
                                          settle=0.02)
            # the hold's sideband is written before its trace line; let the supervisor read it
            time.sleep(0.2)
            os.kill(worker, signal.SIGSTOP)
            stopped = worker
            time.sleep(1.8)
            stopped = None
            try:
                os.kill(worker, signal.SIGCONT)
            except ProcessLookupError as error:
                raise ProtocolError("the worker was ended while it held the rename for a rebuild") from error
            renamed = session.wait_for(lambda item: item.get("id") == renaming, "the held rename's answer")
            edits = (renamed.get("result") or {}).get("changes") or {}
            require(edits, f"a rename held past the deadline was not answered with edits: {renamed!r}")
            require(worker_pid(session) == worker, "the worker was replaced while a request was held")

            # a request that wedges is still ended, and the reloading replacement serves
            os.kill(worker, signal.SIGSTOP)
            stopped = worker
            # a string id, which the crash answer must echo as sent
            wedged = "wedge-1"
            session._send({"jsonrpc": "2.0", "id": wedged, "method": "textDocument/documentSymbol",
                           "params": {"textDocument": {"uri": main.as_uri()}}})
            ended = session.wait_for(lambda item: item.get("id") == wedged, "the wedged request's answer")
            require((ended.get("error") or {}).get("code") == -32802,
                    f"a wedged request was not ended by the deadline: {ended!r}")
            hover = session.request("textDocument/hover", {
                "textDocument": {"uri": main.as_uri()}, "position": {"line": line, "character": column}})
            require(hover.get("result"), f"the replacement did not serve after reloading: {hover!r}")
            notes = [m for m in session.pending if m.get("method") == "window/showMessage"]
            require(len(notes) == 1 and notes[0]["params"]["type"] == 2,
                    f"want one recovered-hang message, got: {notes!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()
            if stopped is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(stopped, signal.SIGKILL)


def run_spare_warmup_after_idle(server: Path, timeout: float) -> None:
    """An idle root warms its cold spare, so the first edit after idle is not cold (#252).

    A root serves from one session and keeps a spare to rebuild into. The spare's
    first build is cold: it has never analyzed the project, so the first edit that
    follows a load rebuilds from scratch even though the load already did that
    work once. During idle the analysis thread has nothing to answer, so it warms
    the cold spare with the snapshot's own input; the edit that follows then
    rebuilds warm.

    The trigger is what this locks in. The pump that schedules the warm-up runs
    when the message queue drains, not only before the next message, so a genuinely
    idle session -- no further traffic -- still warms. Measured against the pump
    running only before the next message, the first post-idle edit here rebuilds in
    warm time, a small fraction of the cold load, and within reach of a second warm
    edit rather than dwarfing it.
    """
    with tempfile.TemporaryDirectory(prefix="mls-warmup-") as directory:
        root = Path(directory).resolve()
        main, text = write_wide_project(root, "warm", 256)
        builds = BuildLog(root / "trace.log")
        session = LspSession(server, root, timeout, builds.env())
        durations = re.compile(r"project: rebuilt .* in (\d+)ms")
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})
            session.diagnostics(main.as_uri(), 1)
            # the inline cold load's own time, the cost the warm-up is meant to remove
            cold = durations.findall(builds.text())
            require(cold, "the cold load did not report a build duration")
            cold_ms = int(cold[-1])

            # no edits follow. the pump must warm the cold spare on the analysis
            # thread going idle; before the trigger fix this only fired at the next
            # message, so an idle session never warmed and this would never settle.
            eventually(lambda: "warming the spare session" in builds.text(),
                       lambda hit: hit, "the spare to warm during idle", timeout)
            builds.settle(1, "the spare warm-up to finish")

            def edit_ms(version: int) -> int:
                before = rebuilt(builds)
                session.notify("textDocument/didChange", {
                    "textDocument": {"uri": main.as_uri(), "version": version},
                    "contentChanges": [{"text": text + f"\n# edit {version}\n"}]})
                eventually(lambda: rebuilt(builds) > before, lambda hit: hit,
                           f"edit {version} to rebuild")
                return int(durations.findall(builds.text())[-1])

            first_ms = edit_ms(2)
            second_ms = edit_ms(3)

            # both edits rebuild warm. a first edit that found a cold spare would
            # rebuild in cold time and dwarf the second; the warm-up keeps it in
            # reach of a warm rebuild instead.
            require(first_ms <= second_ms * 3 + 50,
                    f"first post-idle edit was cold: {first_ms}ms against {second_ms}ms warm "
                    f"(cold load was {cold_ms}ms)")
            require(first_ms < cold_ms,
                    f"first post-idle edit ({first_ms}ms) was no faster than the cold load "
                    f"({cold_ms}ms), so the warm-up did not take")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_option_deadline(server: Path, timeout: float) -> None:
    """`requestDeadlineMs` bounds a wedged request with no environment at all.

    The default is two minutes, so an ignored option shows as this test timing
    out. POSIX only, like `run_hung_worker`, whose wedge it reuses.
    """
    if os.name != "posix":
        print("  option deadline: skipped (needs pgrep and SIGSTOP)")
        return
    with tempfile.TemporaryDirectory(prefix="mls-optdeadline-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "optdeadline", 7)
        session = LspSession(server, root, timeout)
        wedged = None
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {},
                                           "initializationOptions": {"requestDeadlineMs": 1200}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})
            session.diagnostics(main.as_uri(), 1)
            for _ in range(200):
                children = subprocess.run(["pgrep", "-P", str(session.proc.pid)],
                                          capture_output=True, text=True).stdout.split()
                if children:
                    wedged = int(children[0])
                    break
                time.sleep(0.02)
            require(wedged is not None, "no analysis worker to wedge")
            os.kill(wedged, signal.SIGSTOP)
            pending = session.next_id
            session.next_id += 1
            session._send({"jsonrpc": "2.0", "id": pending, "method": "textDocument/documentSymbol",
                           "params": {"textDocument": {"uri": main.as_uri()}}})
            answer = session.wait_for(lambda item: item.get("id") == pending, "the wedged request's answer")
            require((answer.get("error") or {}).get("code") == -32802,
                    f"the option deadline did not end the wedged request: {answer!r}")
            # the replacement worker was given the same options, and answers
            symbols = session.request("textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            require(symbols.get("result"), f"the session did not recover: {symbols!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()
            if wedged is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(wedged, signal.SIGKILL)


def run_rebuild_concurrency(server: Path, timeout: float) -> None:
    """Prove a request is ANSWERED while a project rebuild is still running.

    This is the contract the off-thread rebuild exists for, so it is asserted by
    ordering rather than by a clock: the edit's own diagnostics arrive first, the
    request is answered next, and only then does the republish that follows the
    swap appear. If the rebuild still owned the analysis thread, the request
    could not be answered until it finished, and the republish could not arrive
    after an answer that never came before it.
    """
    with tempfile.TemporaryDirectory(prefix="mls-rebuild-") as directory:
        root = Path(directory).resolve()
        main, text = write_wide_project(root, "wide", 48)
        builds = BuildLog(root / "trace.log")
        session = LspSession(server, root, timeout, builds.env())
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
            builds.settle(0, "the initial load to quiesce")

            edited = text + "\n# one edit, one rebuild\n"
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": main.as_uri(), "version": 2},
                 "contentChanges": [{"text": edited}]},
            )
            # the edit is answered immediately, from editor analysis of the live
            # buffer, while the rebuild it scheduled is still running
            session.diagnostics(main.as_uri(), 2)

            symbols = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": main.as_uri()}})
            require(isinstance(symbols.get("result"), list) and symbols["result"],
                    f"a request during a rebuild was not answered: {symbols!r}")
            # nothing republished while that request was outstanding, so the
            # rebuild had not finished when it was answered
            early = [item for item in session.pending if session.source_diagnostics(item)]
            require(not early,
                    f"the rebuild finished before the request was answered: {early!r}")

            # and the rebuild does land, republishing the document it moved
            session.diagnostics(main.as_uri(), 2)
            builds.settle(0, "the rebuild to quiesce")

            # a burst of edits during a build coalesces into ONE follow-up build,
            # not one per edit. counted from the server's own log, not timed.
            before = builds.scheduled()
            version = 2
            for _ in range(12):
                version += 1
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": text + f"\n# edit {version}\n"}]},
                )
            session.diagnostics(main.as_uri(), version)
            builds.settle(0, "the burst's rebuilds to quiesce")
            after = builds.scheduled()
            require(after - before <= 2,
                    f"a burst of 12 edits started {after - before} rebuilds, not at most 2")

            session.finish()
            finished = True
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
            result = eventually(
                lambda: session.request("textDocument/definition", fwd).get("result"),
                lambda r: isinstance(r, dict) and r.get("uri") == defs.as_uri(),
                "fwd re-export path")
            require(isinstance(result, dict) and result.get("uri") == defs.as_uri(),
                    f"a fwd re-export path did not resolve: {result!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


NAV_MANIFEST = """[project]
id = "nav"
version = "0.1.0"
src = "src"
out = "out/{target.name}/{profile.name}"

[target.linux-x86_64]
isa = "x86_64"
os = "linux"
abi = "sysv64"

[profile.debug]
opt = 0
debug = true
simd = "scalarize"
vectorize = true
float_reassoc = false

[artifact.app]
kind = "bin"
entry = "main.mach"
out = "bin/app"
targets = ["*"]
link = []
need = []
"""

NAV_DEFS = """pub rec Inner { w: i32; }
pub rec Box { v: i32; inner: Inner; }
pub tag Color: u8 { red; blue: i32; }
pub fun make() Color { ret Color.red{}; }
pub fun helper(n: i32) i32 { ret n + 1; }
"""

LONE_BUFFER = """pub fun leaf(n: i32) i32 {
    ret n + 1;
}

pub fun trunk(n: i32) i32 {
    ret leaf(n) + leaf(n + 1);
}
"""

NAV_OTHER = """use nav.defs.helper;

pub fun elsewhere(n: i32) i32 {
    ret helper(n);
}
"""

NAV_MAIN = """use nav.other;
use nav.defs.Box;
use nav.defs.Color;
use nav.defs.make;
use nav.defs.helper;

pub def Handler: fun(i32) i32;

pub rec Table { fn: Handler; }

pub fun local(n: i32) i32 {
    ret helper(n) + helper(n + 1);
}

pub fun pick() Color {
    ret make();
}

pub fun indirect(f: Handler, t: Table) i32 {
    ret f(1) + t.fn(2);
}

pub fun measure(p: *Box) i32 {
    ret p.v;
}

pub fun main() i32 {
    var b: Box;
    val c: Color = make();
    val n: i32   = b.v + b.inner.w;
    ret local(n) + indirect(helper, Table{fn: helper});
}
"""


def write_nav_project(parent: Path) -> tuple[Path, Path, str]:
    """A two-module project covering the navigation features' interesting shapes.

    One module declares a record, a nested record, a tag and two functions; a
    second imports them and calls across the module boundary, directly and through
    a `fun` value; a third calls the same function without being the open buffer or
    the declaring file. Cross-module is the point: a single file would let a walk
    that never leaves the open buffer pass, and the third module is the one that
    neither end of the request names.
    """
    root = parent / "nav"
    (root / "src").mkdir(parents=True)
    (root / "mach.toml").write_text(NAV_MANIFEST, encoding="utf-8")
    main = root / "src" / "main.mach"
    defs = root / "src" / "defs.mach"
    main.write_text(NAV_MAIN, encoding="utf-8")
    defs.write_text(NAV_DEFS, encoding="utf-8")
    (root / "src" / "other.mach").write_text(NAV_OTHER, encoding="utf-8")
    return main, defs, NAV_MAIN


def nav_position(text: str, within: str, needle: str) -> dict[str, Any]:
    """A cursor in the middle of `needle`, on the unique line holding `within`.

    The middle, not one past the start: a one-character name is a real cursor
    target - `var b: Box;` - and start + 1 lands on the colon after it, where
    every positional request answers null for the wrong reason.
    """
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if within in value)
    start = lines[line].index(needle, lines[line].index(within))
    return {"line": line, "character": start + len(needle) // 2}


def nav_range(text: str, needle: str) -> dict[str, Any]:
    """The LSP range of the first occurrence of `needle` in `text`."""
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if needle in value)
    start = lines[line].index(needle)
    return {"start": {"line": line, "character": start},
            "end": {"line": line, "character": start + len(needle)}}


def nav_ranges(text: str, within: str, needle: str) -> list[dict[str, Any]]:
    """Every range of `needle` on the single line holding `within`."""
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if within in value)
    found: list[dict[str, Any]] = []
    start = 0
    while True:
        at = lines[line].find(needle, start)
        if at < 0:
            return found
        found.append({"start": {"line": line, "character": at},
                      "end": {"line": line, "character": at + len(needle)}})
        start = at + len(needle)


NAV_ALIAS = """use h: nav.defs.helper;

pub fun aliased(n: i32) i32 {
    ret h(n);
}
"""


def apply_workspace_edit(changes: dict[str, list[dict[str, Any]]]) -> None:
    """Write a WorkspaceEdit's `changes` to disk. Positions are UTF-16, and the
    fixtures are ASCII, so a column is a character index."""
    for uri, edits in changes.items():
        path = Path(uri_file(uri))
        lines = path.read_text(encoding="utf-8").split("\n")
        for e in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
                        reverse=True):
            start, end = e["range"]["start"], e["range"]["end"]
            require(start["line"] == end["line"], f"a rename edit spans lines: {e!r}")
            line = lines[start["line"]]
            lines[start["line"]] = line[:start["character"]] + e["newText"] + line[end["character"]:]
        path.write_text("\n".join(lines), encoding="utf-8")


def run_rename_validation(server: Path, timeout: float) -> None:
    """A rename is refused when it would break the project or change what it means (#286).

    The property is checked end to end: every rename the server answers with
    edits is applied to a copy of the project, and the compiler must accept the
    result. The names that must be refused are refused with RequestFailed:
    names that are not identifiers, a word the grammar reads as something else
    where it would stand, and a name already bound where the symbol is declared
    or used, whether a module-level name, a built-in type or a local. A
    contextual keyword the grammar accepts in every place the rename writes it
    is allowed, and compiles. A field cannot take the name of another field of
    its record.
    """
    compiler = os.environ.get("MACH_COMPILER") or shutil.which("mach")
    with tempfile.TemporaryDirectory(prefix="mls-renames-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_nav_project(root)
        # a call standing as a statement, where `if` and `ret` read as keywords
        (main.parent / "stmt.mach").write_text(
            "use nav.defs.helper;\n\npub fun twice(n: i32) i32 {\n    helper(n);\n    ret helper(n);\n}\n",
            encoding="utf-8")
        text = "use nav.stmt;\n" + text
        main.write_text(text, encoding="utf-8")
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})
            session.diagnostics(main.as_uri(), 1)

            def rename(position: dict[str, Any], new_name: str) -> dict[str, Any]:
                return session.call("textDocument/rename", {
                    "textDocument": {"uri": main.as_uri()}, "position": position, "newName": new_name})

            def compiles_after(changes: dict[str, list[dict[str, Any]]]) -> str | None:
                if compiler is None:
                    return None
                copy = Path(directory) / "copy"
                if copy.exists():
                    shutil.rmtree(copy)
                shutil.copytree(root / "nav", copy)
                moved = {uri.replace((root / "nav").as_uri(), copy.as_uri(), 1): e for uri, e in changes.items()}
                apply_workspace_edit(moved)
                done = subprocess.run([compiler, "check", str(copy)], capture_output=True, text=True, timeout=timeout)
                return None if done.returncode == 0 else (done.stdout + done.stderr)[:600]

            at_call = nav_position(text, "ret helper(n) + helper(n + 1);", "helper")
            at_param = nav_position(text, "pub fun local(n: i32) i32 {", "(n")
            at_c = nav_position(text, "val c: Color = make();", "c")
            refused = {
                (json.dumps(at_call), ""): "is not a mach identifier",
                (json.dumps(at_call), "has space"): "is not a mach identifier",
                (json.dumps(at_call), "x.y"): "is not a mach identifier",
                (json.dumps(at_call), "1bad"): "is not a mach identifier",
                (json.dumps(at_call), "if"): "would change what the code means where it is written (stmt.mach)",
                (json.dumps(at_call), "ret"): "would change what the code means where it is written (stmt.mach)",
                (json.dumps(at_call), "local"): "is already bound in this module",
                (json.dumps(at_call), "Box"): "is already bound in this module",
                (json.dumps(at_call), "i32"): "names a built-in type",
                (json.dumps(at_call), "n"): "is already bound in a declaration the rename touches",
                (json.dumps(at_param), "helper"): "is already bound in this module",
                (json.dumps(at_c), "n"): "is already bound in a declaration the rename touches",
                (json.dumps(at_c), "b"): "is already bound in a declaration the rename touches",
            }
            for (where, new_name), why in refused.items():
                answer = rename(json.loads(where), new_name)
                error = answer.get("error") or {}
                require(error.get("code") == -32803 and why in error.get("message", ""),
                        f"rename to {new_name!r} was not refused with {why!r}: {answer!r}")

            # allowed: a fresh name, and contextual keywords wherever the grammar
            # takes them. whatever is allowed must compile
            for where, new_name in ((at_call, "assist"), (at_call, "fun"),
                                    (at_param, "count"), (at_param, "ret"), (at_c, "shade")):
                answer = rename(where, new_name)
                if "error" in answer:
                    require(new_name in ("ret", "fun") and answer["error"].get("code") == -32803,
                            f"rename to {new_name!r} was refused: {answer!r}")
                    continue
                changes = (answer.get("result") or {}).get("changes") or {}
                require(changes, f"rename to {new_name!r} produced no edits: {answer!r}")
                broken = compiles_after(changes)
                require(broken is None, f"rename to {new_name!r} was allowed but breaks the project: {broken}")

            # a field cannot take another field's name
            at_field = nav_position(text, "val n: i32   = b.v + b.inner.w;", ".v")
            answer = rename(at_field, "inner")
            require((answer.get("error") or {}).get("code") == -32803
                    and "already a field" in answer["error"].get("message", ""),
                    f"a field rename onto another field was not refused: {answer!r}")
            answer = rename(at_field, "value")
            changes = (answer.get("result") or {}).get("changes") or {}
            require(changes, f"a field rename produced no edits: {answer!r}")
            broken = compiles_after(changes)
            require(broken is None, f"a field rename was allowed but breaks the project: {broken}")
            if compiler is None:
                print("  rename validation: compile check skipped (no mach compiler)")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_cross_module_references(server: Path, timeout: float) -> None:
    """References and rename reach every module that imports the symbol (#246).

    A `use`d binding carries its referent's origin but no declaration of its own,
    so an identity test by declaration alone stopped the walk at the open buffer.
    Asked from an importer, both requests must reach the declaring module, a
    third module that imports the same function, and a module that imports it
    under an alias - where rename rewrites the import path but not the alias.
    """
    with tempfile.TemporaryDirectory(prefix="mls-xrefs-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_nav_project(root)
        other = main.parent / "other.mach"
        alias = main.parent / "alias.mach"
        alias.write_text(NAV_ALIAS, encoding="utf-8")
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            for doc, body in ((alias, NAV_ALIAS), (main, text)):
                session.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": doc.as_uri(), "languageId": "mach",
                                      "version": 1, "text": body}},
                )
                session.diagnostics(doc.as_uri(), 1)

            at_call = {"textDocument": {"uri": main.as_uri()},
                       "position": nav_position(text, "ret helper(n) + helper(n + 1);", "helper")}

            def lines_by_file(locations: list[dict[str, Any]]) -> dict[str, list[int]]:
                found: dict[str, list[int]] = {}
                for loc in locations:
                    found.setdefault(Path(loc["uri"].removeprefix("file://")).name, []).append(
                        loc["range"]["start"]["line"])
                return {k: sorted(v) for k, v in found.items()}

            refs = session.request("textDocument/references",
                                   {**at_call, "context": {"includeDeclaration": True}})["result"]
            by_file = lines_by_file(refs)
            require(set(by_file) == {"main.mach", "defs.mach", "other.mach", "alias.mach"},
                    f"references did not reach every importer: {by_file!r}")
            require(by_file["defs.mach"] == [NAV_DEFS.splitlines().index(
                        next(l for l in NAV_DEFS.splitlines() if l.startswith("pub fun helper")))],
                    f"the declaration is missing: {by_file!r}")
            require(by_file["other.mach"] == [0, 3],
                    f"the third module's import and call are not both reported: {by_file!r}")
            require(by_file["alias.mach"] == [0, 3],
                    f"the aliased module's import and call are not both reported: {by_file!r}")

            edit = session.request("textDocument/rename", {**at_call, "newName": "assist"})["result"]
            changes = {Path(uri.removeprefix("file://")).name: sorted(e["range"]["start"]["line"] for e in edits)
                       for uri, edits in edit.get("changes", {}).items()}
            require(set(changes) == {"main.mach", "defs.mach", "other.mach", "alias.mach"},
                    f"rename did not reach every importer: {changes!r}")
            require(changes["other.mach"] == [0, 3],
                    f"rename missed the third module's import or call: {changes!r}")
            # the alias keeps its own name; only the path that names the function moves
            require(changes["alias.mach"] == [0],
                    f"rename rewrote the alias, or missed its import path: {changes!r}")
            for e in edit["changes"][alias.as_uri()]:
                require(e["newText"] == "assist" and e["range"]["start"]["character"] > len("use h: nav.defs"),
                        f"the aliased import was not rewritten at its path leaf: {e!r}")

            # asked through the alias, rename still renames the declaration and keeps
            # the alias: the guard is the declared name, not the binding's own
            via_alias = session.request("textDocument/rename", {
                "textDocument": {"uri": alias.as_uri()},
                "position": nav_position(NAV_ALIAS, "ret h(n);", "h"),
                "newName": "assist"})["result"]
            alias_changes = {Path(uri.removeprefix("file://")).name: sorted(e["range"]["start"]["line"] for e in edits)
                             for uri, edits in via_alias.get("changes", {}).items()}
            require(alias_changes == changes,
                    f"rename through the alias differs from rename at a call: {alias_changes!r} vs {changes!r}")

            # asked from the declaring module, the answer is the same set
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": defs.as_uri(), "languageId": "mach",
                                  "version": 1, "text": NAV_DEFS}},
            )
            session.diagnostics(defs.as_uri(), 1)
            from_decl = session.request("textDocument/references", {
                "textDocument": {"uri": defs.as_uri()},
                "position": nav_position(NAV_DEFS, "pub fun helper", "helper"),
                "context": {"includeDeclaration": True}})["result"]
            require(lines_by_file(from_decl) == by_file,
                    f"references from the declaration differ from references at a call: "
                    f"{lines_by_file(from_decl)!r} vs {by_file!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_call_hierarchy(server: Path, timeout: float) -> None:
    """Call hierarchy resolves items by position, across modules, including
    calls whose target is not statically known.

    Three things here can only fail against a real project. An item's uri is the
    declaring module's, which for a cross-module callee is a file the editor never
    opened, so item resolution has to reach the project snapshot rather than the
    open-document store - `defs.mach` is never opened in this session. A caller
    that imported the function binds it without a DeclId of its own, so a
    decl-keyed identity test reports that nothing outside the declaring file calls
    it. And a fromRange is a span in the caller's file, which has a different line
    index from the callee's.

    Calls through a `fun` value are reported rather than omitted: the call exists
    and the target does not, and the item says which by its kind and its detail.
    """
    with tempfile.TemporaryDirectory(prefix="mls-callhier-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_nav_project(root)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            capabilities = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}},
            )["result"]["capabilities"]
            require(capabilities.get("callHierarchyProvider") is True,
                    f"call hierarchy is not advertised: {capabilities!r}")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            assert_diagnostics(session.diagnostics(main.as_uri(), 1), False, 1)

            def prepare(within: str, needle: str) -> Any:
                return session.request(
                    "textDocument/prepareCallHierarchy",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": nav_position(text, within, needle)},
                )["result"]

            def one_item(within: str, needle: str) -> dict[str, Any]:
                result = prepare(within, needle)
                require(isinstance(result, list) and len(result) == 1,
                        f"prepare on {needle!r} is not a single item: {result!r}")
                return result[0]

            def calls(item: dict[str, Any], which: str) -> list[dict[str, Any]]:
                result = session.request(
                    f"callHierarchy/{which}Calls", {"item": item},
                )["result"]
                require(isinstance(result, list),
                        f"{which}Calls is not a list: {result!r}")
                return result

            # prepare answers with the declaration, which is in the other module
            helper = one_item("ret helper(n) + helper(n + 1);", "helper")
            require(helper["name"] == "helper" and helper["kind"] == 12,
                    f"prepare named the wrong thing: {helper!r}")
            require(helper["uri"] == defs.as_uri(),
                    f"prepare did not land in the declaring module: {helper!r}")
            require(helper["selectionRange"] == nav_range(NAV_DEFS, "helper"),
                    f"prepare selected the wrong span: {helper!r}")

            # incoming finds the caller in the other module - the item names
            # `defs.mach`, which this session never opened
            incoming = calls(helper, "incoming")
            callers = sorted((entry["from"]["name"], entry["from"]["uri"].rsplit("/", 1)[-1],
                              len(entry["fromRanges"])) for entry in incoming)
            require(callers == [("elsewhere", "other.mach", 1), ("local", "main.mach", 2)],
                    f"incoming did not find both callers: {incoming!r}")
            local_call = next(e for e in incoming if e["from"]["name"] == "local")
            require(local_call["from"]["uri"] == main.as_uri(),
                    f"incoming named the wrong file: {local_call!r}")
            require(local_call["fromRanges"]
                    == nav_ranges(text, "ret helper(n) + helper(n + 1);", "helper"),
                    f"incoming ranges are not the two call sites: {local_call!r}")
            other_call = next(e for e in incoming if e["from"]["name"] == "elsewhere")
            require(other_call["fromRanges"] == nav_ranges(NAV_OTHER, "ret helper(n);", "helper"),
                    f"a caller in a third module indexed the wrong file: {other_call!r}")

            # outgoing is the mirror, and its ranges index the caller's file
            local = one_item("pub fun local(n: i32) i32 {", "local")
            outgoing = calls(local, "outgoing")
            require(len(outgoing) == 1,
                    f"outgoing did not find exactly one callee: {outgoing!r}")
            require(outgoing[0]["to"]["uri"] == defs.as_uri()
                    and outgoing[0]["to"]["selectionRange"] == nav_range(NAV_DEFS, "helper"),
                    f"outgoing named the wrong callee: {outgoing[0]!r}")
            require(outgoing[0]["fromRanges"]
                    == nav_ranges(text, "ret helper(n) + helper(n + 1);", "helper"),
                    f"outgoing ranges do not index the caller's file: {outgoing[0]!r}")

            # two calls to the same function are one entry with two ranges, and
            # distinct callees are distinct entries
            entries = calls(one_item("pub fun main() i32 {", "main"), "outgoing")
            require([e["to"]["name"] for e in entries] == ["make", "local", "indirect"],
                    f"outgoing did not list every callee once: {entries!r}")

            # a call through a `fun` value is reported, and says what it is: a
            # parameter of function type, and a function held in a record field,
            # which resolves to no symbol at all
            through = calls(one_item("pub fun indirect(f: Handler, t: Table) i32 {",
                                     "indirect"), "outgoing")
            require([e["to"]["name"] for e in through] == ["f", "fn"],
                    f"a call through a fun value was dropped: {through!r}")
            for entry in through:
                require(entry["to"]["kind"] == 13,
                        f"an indirect call is not reported as a value: {entry!r}")
                require("not statically known" in entry["to"].get("detail", ""),
                        f"an indirect call does not say its target is unknown: {entry!r}")
            require(through[1]["to"]["uri"] == main.as_uri()
                    and through[1]["to"]["selectionRange"]
                    == nav_ranges(text, "ret f(1) + t.fn(2);", "fn")[0],
                    f"an unresolved callee is not anchored on its call: {through[1]!r}")
            require(through[0]["fromRanges"] == nav_ranges(text, "ret f(1) + t.fn(2);", "f")[:1],
                    f"an indirect fromRange is not the call site: {through[0]!r}")
            require(through[1]["fromRanges"] == nav_ranges(text, "ret f(1) + t.fn(2);", "fn"),
                    f"an unresolved fromRange is not the call site: {through[1]!r}")

            # only a function anchors a hierarchy: a value that holds one is not
            # one, and its incoming calls would be a runtime fact
            for within, needle in (("var b: Box;", "b"),
                                   ("var b: Box;", "Box"),
                                   ("pub def Handler: fun(i32) i32;", "Handler")):
                require(prepare(within, needle) is None,
                        f"prepare minted an item for {needle!r}, which is not a function")

            # an item this server did not mint is answered, not left hanging
            bogus = {"name": "nowhere", "kind": 12, "uri": main.as_uri(),
                     "range": nav_range(text, "pub fun main() i32 {"),
                     "selectionRange": {"start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 0}}}
            require(calls(bogus, "incoming") == [],
                    "an unresolvable item did not answer empty")
            require(calls(bogus, "outgoing") == [],
                    "an unresolvable item did not answer empty")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()

    run_call_hierarchy_standalone(server, timeout)


def run_call_hierarchy_standalone(server: Path, timeout: float) -> None:
    """A buffer belonging to no project still has callers: its own.

    There is no module array to walk here, and answering nothing would be the
    easy reading of "no project". The buffer is the whole world, so it is the
    whole walk - the same fallback `build_refs` makes for references.
    """
    with tempfile.TemporaryDirectory(prefix="mls-callhier-lone-") as directory:
        root = Path(directory).resolve()
        lone = root / "lone.mach"
        lone.write_text(LONE_BUFFER, encoding="utf-8")
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": lone.as_uri(), "languageId": "mach",
                                  "version": 1, "text": LONE_BUFFER}},
            )
            session.diagnostics(lone.as_uri(), 1)

            item = session.request(
                "textDocument/prepareCallHierarchy",
                {"textDocument": {"uri": lone.as_uri()},
                 "position": nav_position(LONE_BUFFER, "pub fun leaf(n: i32) i32 {", "leaf")},
            )["result"]
            require(isinstance(item, list) and len(item) == 1,
                    f"prepare failed on a project-less buffer: {item!r}")

            incoming = session.request(
                "callHierarchy/incomingCalls", {"item": item[0]},
            )["result"]
            require(len(incoming) == 1 and incoming[0]["from"]["name"] == "trunk",
                    f"a project-less buffer reported no callers: {incoming!r}")
            require(incoming[0]["fromRanges"]
                    == nav_ranges(LONE_BUFFER, "ret leaf(n) + leaf(n + 1);", "leaf"),
                    f"the caller's ranges are wrong: {incoming[0]!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_type_definition(server: Path, timeout: float) -> None:
    """typeDefinition lands on the declaration of an expression's type.

    Distinct from definition, which lands on the declaration of the name itself.
    The cases that matter are the ones a record-only back-link would get wrong: a
    `tag` names the type of every `opt` and `res` in the language, and a callee's
    own type is a function type, so a pivot that commits to the tightest typed
    node answers null exactly where a user puts the cursor.
    """
    with tempfile.TemporaryDirectory(prefix="mls-typedef-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_nav_project(root)
        session = LspSession(server, root, timeout)
        finished = False
        try:
            capabilities = session.request(
                "initialize", {"rootUri": root.as_uri(), "capabilities": {}},
            )["result"]["capabilities"]
            require(capabilities.get("typeDefinitionProvider") is True,
                    f"typeDefinition is not advertised: {capabilities!r}")
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            assert_diagnostics(session.diagnostics(main.as_uri(), 1), False, 1)

            def type_definition(within: str, needle: str) -> Any:
                return session.request(
                    "textDocument/typeDefinition",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": nav_position(text, within, needle)},
                )["result"]

            def lands_on(within: str, needle: str, name: str) -> None:
                result = type_definition(within, needle)
                require(isinstance(result, dict),
                        f"typeDefinition on {needle!r} is not a Location: {result!r}")
                require(result.get("uri") == defs.as_uri(),
                        f"typeDefinition on {needle!r} left the declaring module: {result!r}")
                require(result.get("range") == nav_range(NAV_DEFS, name),
                        f"typeDefinition on {needle!r} is not {name!r}: {result!r}")

            # a value lands on its type's declaration, in the other module
            lands_on("var b: Box;", "b", "Box")
            # a written type does too, from the annotation itself
            lands_on("var b: Box;", "Box", "Box")
            # a field access lands on the record declaring the field's type
            lands_on("b.v + b.inner.w", "inner", "Inner")
            # a call reaches its return type, not the callee's function type. in
            # `ret make();` no enclosing binding can stand in for it, so this is
            # the position that proves the call itself is consulted
            lands_on("ret make();", "make", "Color")
            lands_on("val c: Color = make();", "make", "Color")
            # and a tag is a type like any other
            lands_on("val c: Color = make();", "Color", "Color")
            # a parameter is no decl of its own, and its pointer is peeled
            lands_on("measure(p: *Box)", "(p", "Box")
            lands_on("ret p.v;", "p", "Box")

            # a type with no nominal site has nowhere to go
            for within, needle in (("val n: i32   = b.v", "b.v"),
                                   ("val n: i32   = b.v", "v"),
                                   ("ret local(n)", "n"),
                                   ("local(n: i32)", "(n")):
                result = type_definition(within, needle)
                require(result is None,
                        f"typeDefinition on the primitive {needle!r} answered {result!r}")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


KIND_BUFFER = """pub rec R { v: i32; }
pub uni U { a: i32; b: f32; }
pub tag T: u8 { one; two: i32; }
pub def D: fun(i32) i32;
pub val V: i32 = 1;
pub var W: i32 = 2;

pub fun f(n: i32) i32 {
    ret n;
}
"""


def run_document_symbol_kinds(server: Path, timeout: float) -> None:
    """One SymbolKind table, shared by every feature that names a declaration.

    documentSymbol and the call hierarchy both report declarations, and each had
    its own copy of the mapping. They disagreed: a `tag` was SymbolKind.Variable
    on one side, which is what a copy drifts into. `render.symbol_kind` is the one
    spelling, and this pins what it says.
    """
    with tempfile.TemporaryDirectory(prefix="mls-dsymkind-") as directory:
        root = Path(directory).resolve()
        buffer = root / "kinds.mach"
        buffer.write_text(KIND_BUFFER, encoding="utf-8")
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": buffer.as_uri(), "languageId": "mach",
                                  "version": 1, "text": KIND_BUFFER}},
            )
            session.diagnostics(buffer.as_uri(), 1)
            symbols = session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": buffer.as_uri()}},
            )["result"]
            require(isinstance(symbols, list) and symbols,
                    f"documentSymbol returned nothing: {symbols!r}")
            kinds = {entry["name"]: entry["kind"] for entry in symbols}
            expected = {"R": 23, "U": 10, "T": 10, "D": 26, "V": 14, "W": 13, "f": 12}
            for name, kind in expected.items():
                require(kinds.get(name) == kind,
                        f"{name} is SymbolKind {kinds.get(name)!r}, expected {kind}")

            # a tag's cases are its members (#249)
            tag = next(entry for entry in symbols if entry["name"] == "T")
            cases = [(child["name"], child["kind"]) for child in tag.get("children", [])]
            require(cases == [("one", 22), ("two", 22)],
                    f"the tag's cases are not its enum members: {cases!r}")

            # definition on a tag's own name lands on that name
            lines = KIND_BUFFER.splitlines()
            tag_line = next(i for i, value in enumerate(lines) if value.startswith("pub tag T"))
            column = lines[tag_line].index("T:")
            found = session.request("textDocument/definition", {
                "textDocument": {"uri": buffer.as_uri()},
                "position": {"line": tag_line, "character": column}})["result"]
            require(isinstance(found, dict) and found.get("range", {}).get("start")
                    == {"line": tag_line, "character": column},
                    f"definition on a tag's name did not land on it: {found!r}")

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
            started = time.perf_counter()
            broken = session.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": defs.as_uri()}})
            require(time.perf_counter() - started < 1.0,
                    "documentSymbol blocked on project analysis")
            require(isinstance(broken.get("result"), list) and broken["result"],
                    f"documentSymbol needed a loaded project: {broken!r}")
            manifest.write_text(manifest_text, encoding="utf-8")

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


class SyntaxOnlyFeature(NamedTuple):
    """One request answered from a PHASE_PARSE analysis of the open buffer alone."""

    name: str
    method: str
    params: Callable[[str, str], dict[str, Any]]
    valid: Callable[[Any], bool]


# every handler that reaches `analysis.standalone` at `editor.PHASE_PARSE`, which
# today is `document_symbol` by way of `analysis.syntax_tree`. a syntax-only
# handler answers from the buffer and must never touch the project, so each one
# is held to the same latency contract; #221 foldingRange and #222 selectionRange
# join the table when they land.
SYNTAX_ONLY_FEATURES = (
    SyntaxOnlyFeature(
        name="documentSymbol",
        method="textDocument/documentSymbol",
        params=lambda uri, text: {"textDocument": {"uri": uri}},
        valid=lambda result: isinstance(result, list) and bool(result),
    ),
)

# the first requests against a healthy project pay a one-time cold project load,
# so the contract is about steady state and these samples are discarded
SYNTAX_ONLY_WARMUP = 2
# odd, so the median is a sample rather than a mean of two
SYNTAX_ONLY_SAMPLES = 9
# healthy-project median over standalone median. measured in the same process on
# the same machine, so machine load cancels: over seven runs each way the
# absolute medians moved by 5x while the ratio stayed inside 2.77-5.41x against
# mach v5.0.4 and 0.53-1.20x against v5.1.0. 2.0 separates them.
SYNTAX_ONLY_RATIO = 2.0
# under a few milliseconds both paths are dominated by framing rather than by
# analysis and a ratio means nothing. a project reload never measured below
# 6.6ms, so this cannot mask one.
SYNTAX_ONLY_FLOOR = 0.003
# the manifest fingerprint fallback coalesces to at most one scan per 250 ms per
# root, so a request inside that window still sees the previous state
FINGERPRINT_WINDOW = 0.4
# a reload costs what the project's dependencies cost to load, so a fixture with
# no dependencies cannot show one: a dependency-free project reloaded in under
# 3ms even while broken, and the regression was invisible against it. these are
# the std modules the fixture imports, aliased because several share a leaf name.
SYNTAX_ONLY_STD_MODULES = (
    "std.allocator", "std.allocator.arena", "std.allocator.bump", "std.allocator.fixed",
    "std.allocator.heap", "std.allocator.page", "std.chrono.date", "std.chrono.duration",
    "std.chrono.format", "std.chrono.time", "std.collections.bitset", "std.collections.deque",
    "std.collections.heap", "std.collections.map", "std.collections.set",
    "std.collections.slice", "std.collections.sort", "std.collections.vector",
    "std.compress.gzip", "std.compress.inflate", "std.compress.zlib", "std.crypto.ct",
    "std.crypto.hash.crc32", "std.crypto.hash.sha256", "std.crypto.hash.sha512",
    "std.crypto.hash.keccak", "std.crypto.rand", "std.data.json", "std.data.toml",
    "std.encoding.base64", "std.encoding.binary", "std.encoding.hex", "std.filesystem",
    "std.format", "std.input", "std.io.file", "std.io.reader", "std.io.writer",
    "std.log", "std.math", "std.math.bignum", "std.math.bits", "std.math.float",
    "std.math.mat4", "std.math.quat", "std.memory", "std.net.dns", "std.net.ip",
)


def write_std_backed_project(parent: Path) -> tuple[Path, str]:
    """Create an app whose vendored dependency is this repo's own `dep/std`.

    A reload's cost is its dependencies', so the project has to carry a real one
    for the reload to be visible at all. `dep/std` is already on disk in any tree
    that could have built the server under test.
    """
    source = Path(__file__).resolve().parents[1] / "dep" / "std"
    require((source / "mach.toml").is_file(),
            f"the std dependency is not realized: {source} (run `mach dep pull .`)")
    root = parent / "stdapp"
    (root / "src").mkdir(parents=True)
    shutil.copytree(source, root / "dep" / "std",
                    ignore=shutil.ignore_patterns(".git", "out", "dep"))
    (root / "mach.toml").write_text(
        """[project]
id = "stdapp"
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
vectorize = true
float_reassoc = false

[artifact.app]
kind = "bin"
entry = "main.mach"
out = "bin/app"
targets = ["*"]
link = []
need = []

[dep.std]
path = "dep/std"
""",
        encoding="utf-8",
    )
    uses = "".join(f"use m{i}: {module};\n"
                   for i, module in enumerate(SYNTAX_ONLY_STD_MODULES))
    text = f"{uses}\npub fun main() i32 {{\n    ret 0;\n}}\n"
    main = root / "src" / "main.mach"
    main.write_text(text, encoding="utf-8")
    return main, text


def ratio_text(healthy: float, standalone: float) -> str:
    """Format a latency ratio, or say so when the baseline is too small to divide."""
    if standalone <= 0.0:
        return "unmeasurable baseline"
    return f"{healthy / standalone:.2f}x"


def syntax_only_median(session: LspSession, feature: SyntaxOnlyFeature, uri: str,
                       text: str, label: str, samples: int) -> tuple[float, Any]:
    """Median latency over `samples` identical requests, with the last reply.

    perf_counter, not monotonic: monotonic ticks about every 15.6ms on Windows,
    which reads a millisecond-scale request as zero elapsed.
    """
    durations: list[float] = []
    result: Any = None
    for _ in range(samples):
        started = time.perf_counter()
        response = session.request(feature.method, feature.params(uri, text))
        durations.append(time.perf_counter() - started)
        result = response.get("result")
        require(feature.valid(result),
                f"{feature.name} answered nothing usable {label}: {response!r}")
    durations.sort()
    return durations[len(durations) // 2], result


def run_syntax_only_latency(server: Path, timeout: float) -> list[tuple[str, float, float]]:
    """A syntax-only request must not reload the project through the editor session.

    Until mach#3431, `editor.analyze` tore the project down on every call, so a
    request needing nothing but a parse of the open buffer paid a full project
    reload: against this repo, 1586.9ms healthy versus 34.8ms under a manifest
    that cannot load, a 45.6x gap that shipped in 0.18.0 unnoticed. The only
    latency assertion documentSymbol had broke the manifest first, so it measured
    the one path that was never slow.

    Two things make this able to fail where that one could not. The project
    carries a real dependency, because a reload costs what its dependencies cost
    and a dependency-free fixture shows nothing. And the bound is a ratio against
    the standalone path measured in the same run rather than a wall-clock
    threshold, so it neither flakes under machine load nor passes because the
    machine was fast.
    """
    require(SYNTAX_ONLY_FEATURES, "no syntax-only feature is under a latency assertion")
    measured: list[tuple[str, float, float]] = []
    with tempfile.TemporaryDirectory(prefix="mls-synlat-") as directory:
        root = Path(directory).resolve()
        main, text = write_std_backed_project(root)
        manifest = main.parents[1] / "mach.toml"
        manifest_text = manifest.read_text(encoding="utf-8")
        uri = main.as_uri()
        session = LspSession(server, main.parents[1], timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": main.parents[1].as_uri(),
                                           "capabilities": {}})
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            # the fixture must load cleanly, or "healthy" is not healthy and the
            # comparison is between two standalone paths
            assert_diagnostics(session.diagnostics(uri, 1), False, 1)

            for feature in SYNTAX_ONLY_FEATURES:
                syntax_only_median(session, feature, uri, text,
                                   "warming the project load", SYNTAX_ONLY_WARMUP)
                healthy, healthy_result = syntax_only_median(
                    session, feature, uri, text,
                    "against a healthy project", SYNTAX_ONLY_SAMPLES)

                manifest.write_text(manifest_text + "\n[broken\n", encoding="utf-8")
                time.sleep(FINGERPRINT_WINDOW)
                try:
                    standalone, standalone_result = syntax_only_median(
                        session, feature, uri, text,
                        "under a broken manifest", SYNTAX_ONLY_SAMPLES)
                finally:
                    manifest.write_text(manifest_text, encoding="utf-8")
                    time.sleep(FINGERPRINT_WINDOW)

                # a syntax-only answer is a function of the buffer, so losing the
                # project must not change it. without this the ratio could be met
                # by answering less.
                require(healthy_result == standalone_result,
                        f"{feature.name} answered differently once the project was lost")

                allowed = max(standalone * SYNTAX_ONLY_RATIO, SYNTAX_ONLY_FLOOR)
                # require's message is built whether or not it fails, so the
                # ratio cannot be divided here unguarded
                require(healthy <= allowed,
                        f"{feature.name} reloads the project on a healthy root: "
                        f"{healthy * 1000:.1f}ms healthy vs {standalone * 1000:.1f}ms "
                        f"standalone ({ratio_text(healthy, standalone)}, allowed "
                        f"{allowed * 1000:.1f}ms)")
                measured.append((feature.name, healthy, standalone))

            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()
    return measured


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
                # this case is about what PROJECT-backed completion offers, so it
                # waits for the rebuild the edit scheduled. `isIncomplete` is the
                # server's own word for which analysis answered, so it is both
                # the thing waited on and the thing asserted - without it this
                # would silently grade the isolated editor result instead.
                result = settled_result(
                    session, "textDocument/completion",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": anchor_line + 1, "character": 4 + len(probe)}},
                    lambda r: isinstance(r, dict) and r.get("isIncomplete") is False,
                    f"project-backed completion for {probe!r}")
                require(isinstance(result, dict), f"completion is not a list: {result!r}")
                require(result.get("isIncomplete") is False,
                        f"completion answered from isolated analysis: {result!r}")
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

            # a module's public prefix includes its own declarations and re-exports
            exports = complete("exports.")
            require("own" in exports and "answer" in exports,
                    f"a mixed module surface lost an export: {exports!r}")

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


def run_completion_freshness(server: Path, timeout: float) -> None:
    """Queued completion uses current text without consuming stale semantics."""
    if os.name != "posix":
        print("  completion freshness: skipped (rebuild gate needs flock)")
        return
    with tempfile.TemporaryDirectory(prefix="mls-compl-fresh-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "complfresh", 5)
        gate = RebuildGate(root)
        gate.hold()
        session = LspSession(server, root, timeout, env_extra=gate.env)
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

            stale_lines = text.splitlines()
            stale_lines.extend([
                "",
                "rec Fresh { live: i32; }",
                "fun pending() i32 {",
                "    var current: Fresh;",
                "    current.",
                "    ret 0;",
                "}",
            ])
            stale_text = "\n".join(stale_lines) + "\n"
            stale_line = next(i for i, value in enumerate(stale_lines) if value.strip() == "current.")
            version = 1
            notifications = []
            for _ in range(32):
                version += 1
                notifications.append((
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": stale_text}]},
                ))

            pending = session.request_after_notifications(
                notifications,
                "textDocument/completion",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": stale_line, "character": len("    current.")}},
            )
            pending_result = pending.get("result")
            require(isinstance(pending_result, dict)
                    and pending_result.get("isIncomplete") is True,
                    f"stale completion did not identify its isolated result: {pending!r}")
            pending_labels = [item.get("label") for item in pending_result.get("items", [])]
            require("live" in pending_labels,
                    f"current editor member was absent before rebuild: {pending_labels!r}")
            require(session.timings[-1][1] < 1.0,
                    f"isolated completion blocked for {session.timings[-1][1]:.3f}s")

            gate.release()
            session.diagnostics(main.as_uri(), version)
            rebuilt = settled_result(
                session, "textDocument/completion",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": stale_line, "character": len("    current.")}},
                lambda r: isinstance(r, dict) and r.get("isIncomplete") is False,
                "completion after the deferred rebuild")
            require(isinstance(rebuilt, dict) and rebuilt.get("isIncomplete") is False,
                    f"completion stayed isolated after the deferred rebuild: {rebuilt!r}")
            rebuilt_labels = [item.get("label") for item in rebuilt.get("items", [])]
            require("live" in rebuilt_labels,
                    f"deferred rebuild lost the current member: {rebuilt_labels!r}")

            session.finish()
            finished = True
        finally:
            gate.close()
            if not finished:
                session.abort()


def run_completion_alias_while_behind(server: Path, timeout: float) -> None:
    """Completion after a module alias's `.` lists the module while the buffer
    is ahead of the snapshot (#297).

    An isolated editor analysis cannot resolve a `use`, so before this the
    alias was not a symbol there and the answer was empty on every keystroke.
    The `use` names the module as text, and that text is found in the root's
    snapshot. An `isIncomplete` answer is the isolated path's signature: a
    fast machine that served this from a caught-up snapshot fails the check
    instead of passing by accident. A `use` of a module the snapshot has not
    seen yet, one whose file was written after the load, offers nothing until
    the rebuild lands, which is stated here rather than left implied.
    """
    if os.name != "posix":
        print("  completion alias while behind: skipped (rebuild gate needs flock)")
        return
    with tempfile.TemporaryDirectory(prefix="mls-compl-alias-") as directory:
        root = Path(directory).resolve()
        main, _, text = write_project(root, "complalias", 5)
        gate = RebuildGate(root)
        gate.hold()
        session = LspSession(server, root, timeout, env_extra=gate.env)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})
            session.diagnostics(main.as_uri(), 1)

            # a module that exists on disk but not in the snapshot: written after the load
            (main.parent / "later.mach").write_text("pub val arrived: i32 = 1;\n", encoding="utf-8")
            lines = text.splitlines()
            lines[0:0] = ["use later: complalias.later;"]
            lines.extend([
                "",
                "fun typing() i32 {",
                "    rootmod.",
                "    later.",
                "    ret 0;",
                "}",
            ])
            typed = "\n".join(lines) + "\n"
            at = lambda needle: next(i for i, value in enumerate(lines) if value.strip() == needle)
            version = 1
            notifications = []
            for _ in range(32):
                version += 1
                notifications.append((
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": typed}]},
                ))

            def behind(needle: str) -> dict[str, Any]:
                answer = session.request_after_notifications(
                    notifications, "textDocument/completion",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": at(needle), "character": len("    " + needle)}})
                result = answer.get("result")
                require(isinstance(result, dict) and result.get("isIncomplete") is True,
                        f"completion after `{needle}` was not answered while behind, so this proves nothing: {answer!r}")
                return result

            labels = [item.get("label") for item in behind("rootmod.").get("items", [])]
            require("answer" in labels and "take" in labels and "Box" in labels,
                    f"a module alias offered nothing while the buffer was ahead: {labels!r}")
            require("main" not in labels, f"the alias listed names that are not the module's: {labels!r}")

            # a `use` of a module the snapshot does not have offers nothing yet
            require(behind("later.").get("items") == [],
                    "a use the snapshot has not seen offered names from somewhere")

            gate.release()
            session.finish()
            finished = True
        finally:
            gate.close()
            if not finished:
                session.abort()


def run_completion_dependency_alias_while_behind(server: Path, timeout: float) -> None:
    """Completion after a dependency-module alias's `.` while the buffer is ahead.

    The reporter's exact shape: `use prt: std.print;` then `prt.` typed on a
    line the snapshot has not seen yet. The isolated editor analysis resolving
    that buffer loads the dependency's own sources, which grows the session's
    source map and moves its backing array; a stale SourceFile pointer read
    afterwards faulted the worker on macOS, where the freed page is unmapped
    (#297). A local module never triggered it because its source is already
    resident. The worker surviving with an `isIncomplete` answer that carries
    the module's members is the whole point of this case.
    """
    if os.name != "posix":
        print("  completion dependency alias while behind: skipped (rebuild gate needs flock)")
        return
    repo = Path(__file__).resolve().parent.parent
    std = repo / "dep" / "std"
    require((std / "mach.toml").is_file(),
            f"dep/std is not available for the dependency-alias case: {std}")
    with tempfile.TemporaryDirectory(prefix="mls-compl-depalias-") as directory:
        root = Path(directory).resolve()
        source = root / "src"
        source.mkdir(parents=True)
        # mach resolves a dependency from the project's own dep/ tree, so the
        # standard library must be materialized there, not merely referenced
        shutil.copytree(std, root / "dep" / "std", ignore=shutil.ignore_patterns(".git"))
        (root / "mach.toml").write_text(
            f"""[project]
id = "depalias"
version = "0.1.0"
src = "src"
out = "out/{{target.name}}/{{profile.name}}"

[target.linux-x86_64]
isa = "x86_64"
os = "linux"
abi = "sysv64"

[target.darwin-aarch64]
isa = "aarch64"
os = "darwin"
abi = "aapcs64"

[dep.std]
path = "dep/std"

[profile.debug]
opt = 0
debug = true
simd = "scalarize"
vectorize = false
float_reassoc = false

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
        base = ('use prt: std.print;\n\n'
                'pub fun main() i32 {\n'
                '    prt.println("hi");\n'
                '    ret 0;\n'
                '}\n')
        main = source / "main.mach"
        main.write_text(base, encoding="utf-8")
        gate = RebuildGate(root)
        gate.hold()
        session = LspSession(server, root, timeout, env_extra=gate.env)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": base}})
            session.diagnostics(main.as_uri(), 1)

            lines = base.splitlines()
            insert_at = next(i for i, v in enumerate(lines) if "prt.println" in v) + 1
            ahead = lines[:insert_at] + ["    prt."] + lines[insert_at:]
            version = 1
            notifications = []
            # each keystroke carries distinct text so a completed rebuild is always
            # for an older revision: the buffer stays ahead and the completion is
            # answered from the isolated editor analysis, which is the crashing path
            for keystroke in range(32):
                version += 1
                typed = "\n".join(ahead) + f"\n# keystroke {keystroke}\n"
                notifications.append((
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version},
                     "contentChanges": [{"text": typed}]},
                ))

            answer = session.request_after_notifications(
                notifications, "textDocument/completion",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": insert_at, "character": len("    prt.")}})
            result = answer.get("result")
            require(isinstance(result, dict) and isinstance(result.get("items"), list),
                    f"a dependency-module alias completion crashed or errored while behind: {answer!r}")
            # a worker fault on this path returns an error response, which the
            # request helper raises, so reaching here means the worker survived
            # loading the dependency's sources - the regression this guards. The
            # gate holds the rebuild, so the buffer is ahead at completion time
            # and the isolated ahead-path is the one taken: its `isIncomplete`
            # signature is asserted, and on it the aliased module's members must
            # be offered, which is the whole point of that path.
            require(result.get("isIncomplete") is True,
                    f"the buffer did not stay ahead of the snapshot while gated: {answer!r}")
            labels = [item.get("label") for item in result.get("items", [])]
            require("println" in labels and "print" in labels,
                    f"a dependency-module alias offered nothing while the buffer was ahead: {labels!r}")

            gate.release()
            session.finish()
            finished = True
        finally:
            gate.close()
            if not finished:
                session.abort()


def run_completion_type_position_and_imported_members(server: Path, timeout: float) -> None:
    """Completion resolves a receiver by what it is, not where it sits (#321).

    Two shapes were empty. A module alias used as a type qualifier
    (`var v: mod.`) offered nothing, because the receiver was found through an
    expression node and a type position has none. And a value whose record /
    union type is imported (`var v: mod.Rec; v.`) offered nothing while the
    buffer was ahead of the snapshot, because the isolated session cannot type
    an imported value and the only snapshot bridge knew module aliases, not the
    fields of an imported type. Both are answered here, in expression and type
    position and for the symbol-import and alias-qualified spellings of a type.

    An `isIncomplete` answer is the isolated path's signature; asserting it keeps
    a fast machine that served a case from a caught-up snapshot from passing by
    accident. The type-position alias is also checked once caught up, since its
    cause - the expression-only receiver pivot - was not behind-only.
    """
    if os.name != "posix":
        print("  completion receiver resolution: skipped (rebuild gate needs flock)")
        return
    with tempfile.TemporaryDirectory(prefix="mls-compl-recv-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "recv", 5)
        # a public union in the snapshot, so an imported `uni` value can be probed
        defs.write_text(defs.read_text(encoding="utf-8")
                        + "\npub uni Tag { A: i32; B: i32; }\n", encoding="utf-8")
        gate = RebuildGate(root)
        gate.hold()
        session = LspSession(server, root, timeout, env_extra=gate.env)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            session.notify("initialized", {})
            session.notify("textDocument/didOpen", {"textDocument": {
                "uri": main.as_uri(), "languageId": "mach", "version": 1, "text": text}})
            session.diagnostics(main.as_uri(), 1)

            base = text.splitlines()
            version = [1]

            def typed_text(extra: list[str]) -> tuple[str, list[str]]:
                edited = base + [""] + extra
                return "\n".join(edited) + "\n", edited

            def behind(extra: list[str], needle: str) -> list[str]:
                gate.hold()
                body, edited = typed_text(extra)
                line = next(i for i, v in enumerate(edited) if v == needle)
                notifications = []
                for _ in range(32):
                    version[0] += 1
                    notifications.append((
                        "textDocument/didChange",
                        {"textDocument": {"uri": main.as_uri(), "version": version[0]},
                         "contentChanges": [{"text": body}]}))
                answer = session.request_after_notifications(
                    notifications, "textDocument/completion",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": line, "character": len(needle)}})
                result = answer.get("result")
                require(isinstance(result, dict) and result.get("isIncomplete") is True,
                        f"completion for {needle!r} was not answered while behind, so this proves nothing: {answer!r}")
                return [item.get("label") for item in result.get("items", [])]

            def caught_up(extra: list[str], needle: str) -> list[str]:
                gate.release()
                body, edited = typed_text(extra)
                line = next(i for i, v in enumerate(edited) if v == needle)
                version[0] += 1
                session.notify("textDocument/didChange", {"textDocument": {
                    "uri": main.as_uri(), "version": version[0]}, "contentChanges": [{"text": body}]})
                session.diagnostics(main.as_uri(), version[0])
                result = settled_result(
                    session, "textDocument/completion",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": line, "character": len(needle)}},
                    lambda r: isinstance(r, dict) and r.get("isIncomplete") is False,
                    f"project-backed completion for {needle!r}")
                return [item.get("label") for item in result.get("items", [])]

            # #321.1 a module alias used as a type qualifier offers the module's symbols
            tp_lines = ["fun probe() i32 {", "    var a: rootmod.", "    ret 0;", "}"]
            tp = behind(tp_lines, "    var a: rootmod.")
            require("Box" in tp and "answer" in tp and "take" in tp,
                    f"a module alias in type position offered nothing while behind: {tp!r}")
            require("main" not in tp, f"a type-position alias listed names that are not the module's: {tp!r}")
            # the same, caught up: the cause was not behind-only
            tp_ready = caught_up(tp_lines, "    var a: rootmod.")
            require("Box" in tp_ready and "answer" in tp_ready,
                    f"a module alias in type position offered nothing caught up: {tp_ready!r}")

            # #321.2 a value of an imported record type offers its fields while behind,
            # for both a symbol import (`Box`) and an alias-qualified annotation (`rootmod.Box`)
            require(behind(["fun probe() i32 {", "    var c: Box[i32];", "    c.", "    ret 0;", "}"],
                           "    c.") == ["v"],
                    "an imported record value (symbol import) did not offer its field while behind")
            require(behind(["fun probe() i32 {", "    var b: rootmod.Box[i32];", "    b.", "    ret 0;", "}"],
                           "    b.") == ["v"],
                    "an imported record value (alias-qualified) did not offer its field while behind")

            # a value of an imported union offers its cases, and only those
            uni_members = behind(
                ["use recv.defs.Tag;", "fun probe() i32 {", "    var e: Tag;", "    e.", "    ret 0;", "}"],
                "    e.")
            require("A" in uni_members and "B" in uni_members,
                    f"an imported union value did not offer its cases while behind: {uni_members!r}")
            require("main" not in uni_members,
                    f"an imported union value fell back to the file list: {uni_members!r}")

            gate.release()
            session.finish()
            finished = True
        finally:
            gate.close()
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
            started = time.perf_counter()
            require(query("answer") == [], "an unloaded workspace returned symbols")
            require(time.perf_counter() - started < 2.0,
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

            def help_at(probe: str, want: Callable[[Any], bool] | None = None) -> dict[str, Any] | None:
                edited = list(lines)
                edited.insert(anchor_line + 1, "    " + probe)
                version[0] += 1
                session.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": main.as_uri(), "version": version[0]},
                     "contentChanges": [{"text": "\n".join(edited) + "\n"}]},
                )
                session.diagnostics(main.as_uri(), version[0])
                settled = want or (lambda r: isinstance(r, dict) and r.get("signatures"))
                return settled_result(
                    session, "textDocument/signatureHelp",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": anchor_line + 1, "character": 4 + len(probe)}},
                    settled, f"signatureHelp for {probe!r}")

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
            require(help_at("val zz: i64 = 1;", lambda r: r is None) is None,
                    "signatureHelp answered outside a call")
            require(help_at("no_such_function(", lambda r: r is None) is None,
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


def run_doc_structure(server: Path, timeout: float) -> None:
    """Doc structure survives into hover, with either line ending."""
    for crlf in (False, True):
        _run_doc_structure(server, timeout, crlf)


def _run_doc_structure(server: Path, timeout: float, crlf: bool) -> None:
    """A doc comment's line structure has to survive into the hover.

    Doc comments are wrapped prose, so most line breaks are soft and must fold
    into a space or every hover arrives as a column of fragments. A bullet list
    is not soft: it is structure the author wrote on purpose. Folding it in with
    everything else produced one run-on paragraph with `- item` markers stranded
    mid-sentence, which no renderer reads as a list.

    The distinguishing signal is indentation, so the cases that matter are the
    ones where indentation is the only difference: an item versus its own
    wrapped continuation, and a line that returns to the prose margin and ends
    the list. `-1` is checked too, because a marker test that ignores the space
    after it turns arithmetic into bullets.
    """
    with tempfile.TemporaryDirectory(prefix="mls-doc-") as directory:
        root = Path(directory).resolve()
        main, _, _ = write_project(root, "doc", 1)
        text = (
            "# a summary that wraps across\n"
            "# two source lines\n"
            "#\n"
            "# the shapes it handles:\n"
            "#   - a flat item that wraps onto\n"
            "#     a continuation line\n"
            "#   - a second item\n"
            "#       - a nested item under it\n"
            "#\n"
            "# a closing paragraph after the list.\n"
            "pub fun shapes(n: i32) i32 { ret n; }\n"
            "\n"
            "# prose then a list with no blank line between\n"
            "#   - immediately after\n"
            "# and prose right back at the margin.\n"
            "pub fun tight(n: i32) i32 { ret n; }\n"
            "\n"
            "# not a list: -1 is a value and 1.5 is a number\n"
            "pub fun plain(n: i32) i32 { ret n; }\n"
            "\n"
            "pub fun main() i32 { ret shapes(1) + tight(2) + plain(3); }\n"
        )
        # Both line endings, because a carriage return belongs to the line
        # ending and not to the line: a file saved with CRLF used to drop a
        # stray control byte into the middle of the rendered prose, and on
        # Windows `write_text` produces CRLF unless told otherwise, which is how
        # this was found.
        eol = "\r\n" if crlf else "\n"
        main.write_text(text.replace("\n", eol), encoding="utf-8", newline="")
        lines = text.splitlines()

        session = LspSession(server, root, timeout)
        try:
            session.request(
                "initialize",
                {"rootUri": root.as_uri(),
                 "capabilities": {"textDocument": {"hover": {"contentFormat": ["markdown"]}}}},
            )
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text.replace("\n", eol)}},
            )
            session.diagnostics(main.as_uri(), 1)

            call = next(i for i, v in enumerate(lines) if v.startswith("pub fun main"))

            def hover_of(name: str) -> str:
                column = lines[call].index(name + "(") + 2
                answer = session.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": call, "character": column}})
                value = (answer.get("result") or {}).get("contents", {}).get("value")
                require(value, f"no hover for {name}: {answer!r}")
                return value

            shapes = hover_of("shapes")
            body = shapes.split("```")[-1]
            # the wrapped summary still folds: a hover of one-line fragments is
            # the failure this renderer exists to avoid
            require("a summary that wraps across two source lines" in body,
                    f"a wrapped line was not folded: {body!r}")
            # each item starts a line, and its own wrapped continuation folds
            require("\n- a flat item that wraps onto a continuation line" in body,
                    f"a list item did not start a line: {body!r}")
            require("\n- a second item" in body, f"the second item was lost: {body!r}")
            # indentation deeper than the item above it is a nested list, kept
            # relative to the list's own first marker
            require("\n    - a nested item under it" in body,
                    f"nesting was flattened: {body!r}")
            # and the list is closed before the paragraph that follows it, or
            # that paragraph is absorbed into the final item
            require("\n\na closing paragraph after the list." in body,
                    f"the list did not end: {body!r}")

            tight = hover_of("tight").split("```")[-1]
            require("\n- immediately after" in tight,
                    f"a list directly after prose was folded in: {tight!r}")
            require("\n\nand prose right back at the margin." in tight,
                    f"returning to the margin did not end the list: {tight!r}")

            plain = hover_of("plain").split("```")[-1]
            require("\n-" not in plain and "-1 is a value" in plain,
                    f"arithmetic was rendered as a list: {plain!r}")

            session.finish()
        finally:
            with contextlib.suppress(Exception):
                session.abort()


def run_doc_components(server: Path, timeout: float) -> None:
    """Doc components are named by kind and attributed, with either line ending."""
    for crlf in (False, True):
        _run_doc_components(server, timeout, crlf)


def _run_doc_components(server: Path, timeout: float, crlf: bool) -> None:
    """A doc block's component lines describe named parts, and hover must say so.

    The spec gives each declaration element its own component identifier: a
    parameter by name, a generic as `[T]`, a field by name, the return as `ret`.
    Two things follow that were not being done.

    A declaration's hover should present them as what they are. Listing `ret`
    among the parameters read as though the function took an argument called
    `ret`, and a bare `---` rule said nothing about what the list below it was.

    And hovering one of those parts should show the line written for it. A field
    already did; a parameter did not, so documentation an author wrote for an
    argument was reachable only by hovering the function and reading the list.
    """
    with tempfile.TemporaryDirectory(prefix="mls-comp-") as directory:
        root = Path(directory).resolve()
        main, _, _ = write_project(root, "comp", 1)
        text = (
            "# a documented record\n"
            "# ---\n"
            "# width:  how wide the thing is\n"
            "# height: how tall the thing is\n"
            "pub rec Box { width: i32; height: i32; }\n"
            "\n"
            "# a documented union\n"
            "# ---\n"
            "# left_:  the left one\n"
            "# right_: the right one\n"
            "pub uni Side { left_: i32; right_: i32; }\n"
            "\n"
            "# a documented function\n"
            "# ---\n"
            "# [T]:   the element type\n"
            "# scale: how much to scale by, described\n"
            "#        across two wrapped lines\n"
            "# ret:   the scaled area\n"
            "pub fun area[T](scale: i32) i32 {\n"
            "    var copy: T;\n"
            "    ret scale;\n"
            "}\n"
            "\n"
            "# only a return is documented\n"
            "# ---\n"
            "# ret: just the answer\n"
            "pub fun answer() i32 { ret 1; }\n"
            "\n"
            "pub fun main() i32 {\n"
            "    var b: Box;\n"
            "    b.width = 1;\n"
            "    ret area[i32](b.width) + answer();\n"
            "}\n"
        )
        # Written with the endings it is sent with, and run under both. A CRLF
        # buffer used to get no component block at all: mach's doc parser trimmed
        # only spaces and tabs before matching `# ---`, so the `\r` left the
        # separator four characters wide and it never matched, taking every
        # parameter, field and return description out of hover on the platform
        # where editors write CRLF. Fixed in briar-systems/mach#3072, and pinned
        # here because nothing in this repository would notice it coming back.
        eol = "\r\n" if crlf else "\n"
        text = text.replace("\n", eol)
        main.write_text(text, encoding="utf-8", newline="")
        lines = text.splitlines()

        session = LspSession(server, root, timeout)
        try:
            session.request(
                "initialize",
                {"rootUri": root.as_uri(),
                 "capabilities": {"textDocument": {"hover": {"contentFormat": ["markdown"]}}}},
            )
            session.notify("initialized", {})
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)

            def hover_at(linepat, needle, off=2):
                ln = next(i for i, v in enumerate(lines) if linepat in v)
                answer = session.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": ln, "character": lines[ln].index(needle) + off}})
                value = (answer.get("result") or {}).get("contents", {}).get("value")
                require(value, f"no hover for {needle!r} on {linepat!r}: {answer!r}")
                return value

            EM = "\u2014"

            # a declaration presents its components under the word that names them
            fn = hover_at("ret area[i32](b.width)", "area")
            require("**Parameters**" in fn, f"components were not named: {fn!r}")
            require(f"- `scale` {EM} how much to scale by, described across two wrapped lines" in fn,
                    f"a wrapped component did not fold under its own bullet: {fn!r}")
            require("- `[T]`" in fn, f"the generic component was dropped: {fn!r}")
            # the return is not one of the inputs
            require(f"**Returns** {EM} the scaled area" in fn,
                    f"the return was not given its own line: {fn!r}")
            require("- `ret`" not in fn, f"the return was listed as a parameter: {fn!r}")

            rec = hover_at("var b: Box;", "Box")
            require("**Fields**" in rec, f"a record's components are fields: {rec!r}")
            uni = hover_at("pub uni Side", "Side")
            require("**Variants**" in uni, f"a union's components are variants: {uni!r}")

            # a function with only a return gets no empty parameter heading
            only = hover_at("ret area[i32](b.width) + answer()", "answer")
            require(f"**Returns** {EM} just the answer" in only, f"missing return: {only!r}")
            require("**Parameters**" not in only,
                    f"an empty parameter list was announced: {only!r}")

            # and each named part carries its own line, not the whole block
            param = hover_at("ret scale;", "scale")
            require("how much to scale by, described across two wrapped lines" in param,
                    f"a parameter was not attributed: {param!r}")
            require("**Parameters**" not in param,
                    f"hovering a parameter dumped the whole block: {param!r}")

            generic = hover_at("var copy: T;", "T", 0)
            require("the element type" in generic,
                    f"a generic was not attributed: {generic!r}")

            field = hover_at("b.width = 1;", "width")
            require("how wide the thing is" in field,
                    f"a field was not attributed: {field!r}")

            session.finish()
        finally:
            with contextlib.suppress(Exception):
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


def run_failed_rebuild_keeps_serving(server: Path, timeout: float) -> None:
    """A rebuild that fails must leave the previous snapshot answering.

    The case that matters is a SIBLING buffer moving. The failed attempt is
    recorded against the root while the snapshot revision is not, so a root-wide
    staleness test would call the root stale forever with nothing left to
    schedule - every cross-module feature dead until some unrelated edit
    happened to succeed. The buffer being asked about never moved, and the last
    good snapshot still describes it exactly.
    """
    with tempfile.TemporaryDirectory(prefix="mls-failed-rebuild-") as directory:
        root = Path(directory).resolve()
        main, defs, text = write_project(root, "keep", 13)
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
            defs_text = defs.read_text(encoding="utf-8")
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": defs.as_uri(), "languageId": "mach",
                                  "version": 1, "text": defs_text}},
            )
            session.diagnostics(defs.as_uri(), 1)
            assert_definition(session, main, defs, text)

            manifest = main.parents[1] / "mach.toml"
            original = manifest.read_text(encoding="utf-8")
            manifest.write_text(original + "\n[broken\n", encoding="utf-8")
            # past the 250 ms fingerprint-scan window, or nothing rescans
            time.sleep(0.4)
            session.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": defs.as_uri(), "version": 2},
                 "contentChanges": [{"text": defs_text + "\npub val extra: i32 = 1;\n"}]},
            )
            session.diagnostics(defs.as_uri(), 2)
            warning = session.wait_for(
                lambda item: (item.get("method") == "window/showMessage"
                              and isinstance(item.get("params"), dict)
                              and "failed to load project"
                              in str(item["params"].get("message", ""))),
                "failed rebuild warning",
            )
            require(warning["params"].get("type") == 2,
                    f"load failure was not reported as a warning: {warning!r}")

            assert_definition(session, main, defs, text)

            manifest.write_text(original, encoding="utf-8")
            session.finish()
            finished = True
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
            session.assert_no_message(
                lambda item: item.get("id") == registration.get("id"),
                "reply to accepted watcher registration",
            )
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": text}},
            )
            session.diagnostics(main.as_uri(), 1)
            assert_definition(session, main, defs, text)

            lines = text.splitlines()
            line = next(i for i, value in enumerate(lines) if "watched" in value and "use " not in value)
            character = lines[line].index("watched") + 1

            def poke() -> None:
                """Trigger the fingerprint scan that notices the on-disk change.

                The scan runs inside a request and only schedules the rebuild, so
                one request schedules and a later one observes. Sleeping past the
                250 ms scan window is what makes the scan happen at all; the
                polling assertions that follow are what make the result visible.
                """
                session.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": main.as_uri()},
                     "position": {"line": line, "character": character}},
                )

            # the edit must keep the project compiling: mach 5.0 releases a
            # project whose sema phase is rejected (briar-systems/mach#3337)
            changed = defs.read_text(encoding="utf-8").replace(
                "pub val watched: i32 = 9;", "pub val watched: i32 = 99;")
            defs.write_text(changed, encoding="utf-8")
            time.sleep(0.3)
            poke()
            hover = settled_result(
                session, "textDocument/hover",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": line, "character": character}},
                lambda r: "watched: i32 = 99" in json.dumps(r),
                "hover reflecting the on-disk change")
            require("watched: i32 = 99" in json.dumps(hover),
                    f"active watcher suppressed source fingerprint fallback: {hover!r}")

            broken = changed + "use watch.missing.nope;\n"
            defs.write_text(broken, encoding="utf-8")
            time.sleep(0.3)
            poke()
            failed = settled_result(
                session, "textDocument/definition",
                {"textDocument": {"uri": main.as_uri()},
                 "position": {"line": line, "character": character}},
                lambda r: r is None, "broken on-disk source stops resolving")
            require(failed is None,
                    f"broken on-disk source retained a stale snapshot: {failed!r}")

            defs.write_text(changed, encoding="utf-8")
            time.sleep(0.3)
            poke()
            repaired = definition(session, main, text, "watched")
            require(repaired.get("uri") == defs.as_uri(),
                    f"failed root did not retry after disk source repair: {repaired!r}")
            session.finish()
            finished = True
        finally:
            if not finished:
                session.abort()


def run_response_envelopes(server: Path, timeout: float) -> None:
    """Responses of every legal id type are ignored, not answered."""
    with tempfile.TemporaryDirectory(prefix="mls-response-") as directory:
        root = Path(directory).resolve()
        session = LspSession(server, root, timeout)
        finished = False
        try:
            session.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}})
            response_ids: tuple[str | int | None, ...] = ("foreign", 42, None)
            session._send({"jsonrpc": "2.0", "id": response_ids[0], "result": None})
            session._send({"jsonrpc": "2.0", "id": response_ids[1],
                           "error": {"code": -32601, "message": "unknown request"}})
            session._send({"jsonrpc": "2.0", "id": response_ids[2], "result": None})
            session.assert_no_message(
                lambda item: ("id" in item
                              and any(item["id"] == response_id for response_id in response_ids)),
                "reply to a server response",
            )

            invalid_requests = (
                {"jsonrpc": "2.0", "result": None},
                {"jsonrpc": "2.0", "id": "missing-method"},
                {"jsonrpc": "2.0", "id": "both-response-fields", "result": None,
                 "error": {"code": -32601, "message": "unknown request"}},
            )
            for invalid_request in invalid_requests:
                invalid_id = invalid_request.get("id")
                session._send(invalid_request)
                invalid = session.wait_for(
                    lambda item: item.get("id") == invalid_id,
                    "invalid request response",
                )
                error = invalid.get("error")
                require(isinstance(error, dict) and error.get("code") == -32600,
                        f"a method-less non-response was not rejected: {invalid!r}")
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
            create = session.wait_for(
                lambda item: item.get("method") == "window/workDoneProgress/create",
                "progress token creation",
            )
            session.respond_result(create)
            session.assert_no_message(
                lambda item: item.get("id") == create.get("id"),
                "reply to progress token creation",
            )
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
        print("  progress: orphan close skipped (needs pgrep and SIGKILL)")
        return

    with tempfile.TemporaryDirectory(prefix="mls-prog-orphan-") as directory:
        root = Path(directory).resolve()
        # a load slow enough to be interrupted part-way. The window scales with
        # how loaded the machine is, and so does the time to end the worker, so
        # this does not get tighter under CI contention. a load is never ended by
        # the request deadline (#284), so the worker is killed outright
        slow = slow_project(root, "progorphan", 5)
        main, body = slow.main, slow.text
        session = LspSession(server, root, timeout)
        try:
            session.request(
                "initialize",
                {"rootUri": root.as_uri(),
                 "capabilities": {"window": {"workDoneProgress": True}}},
            )
            session.notify("initialized", {})
            # resolved before the load starts, so ending the worker is one
            # syscall rather than a process lookup inside the window
            worker = worker_pid(session)
            session.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": main.as_uri(), "languageId": "mach",
                                  "version": 1, "text": body}},
            )
            begin = session.wait_for(
                lambda item: (item.get("method") == "$/progress"
                              and item["params"]["value"]["kind"] == "begin"),
                "a progress report for the cold load")
            os.kill(worker, signal.SIGKILL)
            token = begin["params"]["token"]

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
        run_version(server, args.timeout)
        run_settings(server, args.timeout)
        run_manifest_notes(server, args.timeout)
        run_position_encoding(server, args.timeout)
        run_option_deadline(server, args.timeout)
        run_deadline_spares_load(server, args.timeout)
        run_spare_warmup_after_idle(server, args.timeout)
        run_cross_module_references(server, args.timeout)
        run_rename_validation(server, args.timeout)
        run_rebuild_concurrency(server, args.timeout)
        run_stale_hover(server, args.timeout)
        run_stale_strict_requests(server, args.timeout)
        run_stale_held_release(server, args.timeout)
        run_stale_refresh(server, args.timeout)
        run_stale_diagnostics(server, args.timeout)
        run_disk_change_during_build(server, args.timeout)
        run_failed_rebuild_keeps_serving(server, args.timeout)
        run_active_watcher_fallback(server, args.timeout)
        run_response_envelopes(server, args.timeout)
        run_import_navigation(server, args.timeout)
        run_document_symbol_hierarchy(server, args.timeout)
        run_document_symbol_kinds(server, args.timeout)
        run_type_definition(server, args.timeout)
        run_call_hierarchy(server, args.timeout)
        syntax_only = run_syntax_only_latency(server, args.timeout)
        run_completion_context(server, args.timeout)
        run_completion_freshness(server, args.timeout)
        run_completion_alias_while_behind(server, args.timeout)
        run_completion_dependency_alias_while_behind(server, args.timeout)
        run_completion_type_position_and_imported_members(server, args.timeout)
        run_document_highlight(server, args.timeout)
        run_workspace_symbol(server, args.timeout)
        run_signature_help(server, args.timeout)
        run_inlay_hints(server, args.timeout)
        run_semantic_tokens(server, args.timeout)
        run_cancellation(server, args.timeout)
        run_incremental_sync(server, args.timeout)
        run_code_actions(server, args.timeout)
        run_hover_presentation(server, args.timeout)
        run_doc_structure(server, args.timeout)
        run_doc_components(server, args.timeout)
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
    print("  a request is answered while a project rebuild is still running")
    print("  an idle root warms its cold spare, so the first edit after idle is warm")
    print("  a burst of edits during a build coalesces into one follow-up build")
    print("  a failed rebuild leaves the previous snapshot answering")
    print("  use / fwd import paths navigate to their declarations")
    print("  documentSymbol nests members, and reflects edits through its cached parse")
    print("  one SymbolKind table: every feature that names a declaration agrees")
    print("  typeDefinition lands on a type's declaration: record, nested field, tag, return type")
    print("  call hierarchy resolves items across modules, and reports calls through fun values")
    print("  a syntax-only request answers from the buffer without reloading the project")
    print("  completion answers for the cursor: members, exports, prefixes")
    print("  queued completion uses current editor analysis before the deferred rebuild")
    print("  documentHighlight classifies reads and writes in the active file")
    print("  workspace/symbol searches loaded roots, best matches first")
    print("  signatureHelp tracks the active argument through incomplete calls")
    print("  inlayHint names literal arguments at multi-parameter calls")
    print("  semanticTokens decode in order, within the legend and the file")
    print("  a withdrawn request is answered RequestCancelled")
    print("  incremental sync patches ranges, ordered, in UTF-16 columns")
    print("  codeAction offers the compiler's own fixes as applicable edits")
    print("  hover renders headers, expression types, and the client's format")
    print("  a doc comment's lists and paragraphs survive into the hover")
    print("  doc components are named by kind, and each part carries its own line")
    print("  clean EOF after shutdown: exit 0")
    print("  all five lifecycle endings terminate with the documented code")
    print("  a worker crash is answered, explained, and replayed into a replacement")
    print("  a wedged worker is ended, answered ServerCancelled, and recovered")
    print("  a cold load reports progress once, and an abandoned report is closed")
    print("  tracing keeps source out of the log unless asked for, and caps it")
    print("  malformed/oversized frames: 8 rejected with exit 1")
    print("  closed stdout reader with inherited SIG_IGN: exit 1")
    print("  closed stdout reader: exit 1")
    for name, healthy, standalone in syntax_only:
        print(f"  {name} steady state: {healthy * 1000:.1f}ms healthy, "
              f"{standalone * 1000:.1f}ms standalone ({healthy / standalone:.2f}x)")
    for label, duration in timings:
        print(f"  {label}: {duration:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
