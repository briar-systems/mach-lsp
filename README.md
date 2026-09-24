# mach-lsp

A language server for the [Mach](https://github.com/briar-systems/mach) programming
language, built directly on Mach's retained compiler frontend and single-file
editor APIs.

## Status

mach-lsp implements lifecycle, incremental text synchronization, diagnostics
(including a project's own, on its `mach.toml`), hover, definition, type
definition, references, rename and prepareRename, document highlight, document
symbols, folding ranges, selection ranges, workspace symbols, call hierarchy,
semantic tokens, inlay hints, signature help, quick-fix code actions, and
completion.

Project documents are analyzed by the compiler's retained frontend API. Each
manifest root owns a stable long-lived compiler Session and one current Project
snapshot; open filesystem documents retain their own text and monotonic revision
and enter the compiler load walk as path overlays and extra module roots. The
selected primary artifact supplies the target, profile, defines and `$project`.
The walk loads every artifact's entry, the union `mach build` builds across the
project, so a module reads the `$bin` of the artifact whose entry reaches it and
an `embed` of `{artifact.<id>.out}` resolves against what any artifact needs.
Modules only an artifact for another target reaches are gated out, as the build
gates them. Resolve, sema, generic instantiation, and diagnostics therefore
come from the same compiler-owned ModuleEntry rather than LSP copies of compiler
internals.

The reading thread owns stdin and nothing else. Every message, parsing
included, is handled on one worker thread that owns the sessions, snapshots,
document registry, and feature handlers, so compiler state has exactly one owner
and needs no locking. A cold analysis therefore cannot stop the server from
reading the cancellation, edit, or shutdown behind it, and cannot block the
client mid-write when a `didChange` fills the pipe. A change whose analysis is
superseded by input already queued is coalesced: the text and revision are
recorded, the analysis is deferred, and the debt is paid when the queue drains,
so a burst of keystrokes costs one analysis of the newest text rather than one
per revision.

Roots are independent identity domains, so projects with colliding FQNs can be
queried and rebuilt in either order. Dependency modules already present in an
ancestor graph route to that graph read-only; unrelated nested projects remain
isolated. Files outside a project use the upstream single-file editor API.

Diagnostics own the analysis rather than reporting whichever snapshot a previous
request happened to leave behind: open and change drive the load, so what the
editor shows matches `mach build` from the first notification and does not move
because hover or definition was requested. Documents outside any project fall
back to standalone parse diagnostics. Republishing is scoped to the root that
changed, so an edit in one project does not emit a notification for every open
buffer in another. Watch registration is
considered active only after the client acknowledges it; `didSave`, watched-file
notifications, manifest/lock mtimes, and exact content fingerprints of previously
loaded source paths all drive invalidation and retry. Fingerprint scans are
coalesced to at most once per 250 ms per root, bounding missed-event detection
without hashing a graph on every request. Source overlays are mirrored
under canonical and manifest-raw POSIX spellings (including `src = "./src"`).
Portable Windows/UNC canonicalization is tracked by #157, mach#2998, and
mach-std#472.

While a root rebuilds, the edited buffer is still answered from the snapshot
it has, read through the edits since that snapshot (#251). `textdiff` compares
the snapshot's text with the buffer line by line and narrows each difference to
its bytes. Every position going out is carried across those windows, and a
position touching one answers nothing rather than a place the client no longer
has. Hover, definition, typeDefinition, highlight, signature help, inlay hints,
semantic tokens and the call hierarchy answer this way, and clients that
support it are asked to refresh tokens and hints once the rebuild lands.
References, rename, prepareRename and code actions must not answer from earlier
text, so they are held until a snapshot covers every buffer of their root, and
answered then. A cancel answers a held request at once, and closing its document
answers it with ContentModified. Diagnostics show the buffer's own syntax errors
when it has any, and otherwise the snapshot's semantic diagnostics that avoid
the edits.

Cross-module references and rename walk the retained graph. Rename is restricted
to project-owned declarations, so vendored dependency sources remain read-only,
and renaming a dependency's symbol is refused with `RequestFailed`. A rename is
also refused when its result would not mean what the code meant before: a new
name that is not a mach identifier, one the grammar would read as something
else where it is written (a call statement renamed to `ret`), or one already
bound where the symbol is declared or used. That last check is conservative:
a local of the new name anywhere in a top-level declaration the rename touches
refuses it, because mach does not expose its scopes. A field cannot be renamed
to the name of another field of its record.
Completion offers, by prefix, every name the document's resolve table binds,
whether or not it is in scope at the cursor. After a `.` it offers a module
alias's public symbols, or the fields of the record or union the receiver has.
While the buffer is ahead of the project's last analysis, the alias's module
is taken from that analysis by name, so the list is there while you type. The
members of a receiver's type are read the same way, from the last analysis,
with one exception (#367): when the document declaring that type is open and
its buffer is ahead of the snapshot, the field names, written annotations and
tag cases come from a parse of that buffer, so a member you just typed is
offered on the next request rather than after the rebuild. The parse contributes
text only, nothing from it is joined to the snapshot, and the answer stays
`isIncomplete` until the rebuild lands. A document that is not open has no
buffer to read, so a change to it waits for the rebuild.

The first semantic request still performs a synchronous whole-project frontend
analysis. Syntax-only document symbols, folding ranges and selection ranges do
not pay that cost; moving semantic work off the request path is tracked by #143.

## Known limits

These are the costs of the current design, not defects awaiting a fix. The
figures are from this repository, which analyzes the whole compiler and
standard library (`dep/mach`, `dep/std`): a release build on mach 5.4.0 with
std 4.0.0, on an 8-core Ryzen 7 5800X3D. Smaller projects pay proportionally
less.

| cost | figure | why |
| --- | --- | --- |
| first semantic answer after opening a project | ~26 s | the first load analyzes the whole project before any semantic request can be answered (#143) |
| first rebuild after that load | ~26 s | a root keeps two sessions, and the second is cold until its first build (#252) |
| every later rebuild | ~1.2-1.4 s | a rebuild still walks the whole project to find what an edit changed (#250) |
| analysis worker memory (resident plus swap) | ~860 MiB after the first load; ~1.7 GiB peak while the spare session builds for the first time; ~1.35 GiB steady once both sessions have built, flat over 30 further edits | the two sessions are the price of rebuilds that never block requests (#248) |

While a rebuild runs, the edited buffer keeps answering from the snapshot it
has (see above), so neither rebuild figure is time without answers. Syntax-only
features never wait on analysis. What remains blocked is the first load.
Semantic requests made during it wait for it to finish. The server keeps reading
input throughout, so it never blocks the editor mid-write, and edits made
meanwhile are coalesced into one analysis.

Positions are UTF-16 code units, as LSP defines by default. The server does not
negotiate `positionEncoding` (#269).

## Building

The compiler and standard library are vendored under `dep/` as git submodules
and declared as git dependencies in `mach.toml`. Pull them, then build with the
Mach toolchain:

```sh
mach dep pull . # vendor dep/mach and dep/std
mach build .    # compile the server
```

The server binary is produced at `out/linux-x86_64/debug/bin/mls`.

## Installing

Each release carries a prebuilt server for every supported platform. Download
the archive for your platform, check it against `SHA256SUMS`, and put `mls` on
your `PATH`:

```sh
v=1.0.0 t=x86_64-linux
curl -LO https://github.com/briar-systems/mach-lsp/releases/download/v$v/mls-$v-$t.tar.gz
curl -LO https://github.com/briar-systems/mach-lsp/releases/download/v$v/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf mls-$v-$t.tar.gz mls && install -Dm755 mls ~/.local/bin/mls
mls --version
```

Or build it yourself and copy that binary instead:

```sh
install -Dm755 out/linux-x86_64/debug/bin/mls ~/.local/bin/mls
```

### Release assets

The names are a contract: editor extensions download by them.

| asset | contents |
| --- | --- |
| `mls-<version>-<platform>.tar.gz` | `mls` and `LICENSE`, for `x86_64-linux`, `aarch64-linux`, `aarch64-darwin`, `x86_64-darwin` |
| `mls-<version>-x86_64-windows.zip` | `mls.exe` and `LICENSE` |
| `RELEASES.json` | every installable mls release and the mach version it links |
| `SHA256SUMS` | the SHA-256 of every other asset, in `sha256sum` format |

`<version>` has no leading `v`. `mls --version` prints
`mls <version> (mach <compiler version>)`. `initialize` reports the same
`<version>` as `serverInfo.version`, and the compiler version as
`serverInfo.mach`. Every shipped platform runs the full protocol suite natively
in CI. `riscv64-linux` is a build target without a native runner and is not
shipped.

`RELEASES.json` is one JSON object, newest release first, mapping each mls
version to the mach version that release links:

```json
{"0.21.0": "5.4.0", "0.20.0": "5.4.0", "0.19.0": "5.2.1"}
```

A server refuses a project whose `[project].mach` range excludes its mach, so
an installer that cannot list releases reads the newest release's
`RELEASES.json` to find the newest mls a project accepts. A listed version is
an installable one: its release carries every archive above and `SHA256SUMS`.
A tag with no release, or a release missing an archive, is not listed, so a
prebuilt server exists for every entry. Every release carries the complete
map. It is generated from the release tags when a release is cut, and the
release fails if a listed version lacks its assets or if its own entry
disagrees with its binary. The format does not change from 1.0 on.

Then point your editor's LSP client at `mls`; the server speaks the LSP base
protocol over stdin/stdout.

## Command line

The public interface is two invocations:

| invocation | behaviour |
| --- | --- |
| `mls` | the language server, speaking LSP over stdin/stdout |
| `mls --version` | prints `mls <version> (mach <compiler version>)` and exits |

`mls --worker` is **private**. The server re-launches itself with it to run the
analysis in a supervised child process, so a compiler fault is a child exit the
editor never sees. It is not a stable interface: its name, its arguments and
its behaviour may change in any release. Editors and scripts must not pass it.

## Configuration

The server reads its configuration once, from the `initialize` request. Every
setting has an environment variable behind it, and the order is: the
`initializationOptions` key, then the environment variable, then the default.

| `initializationOptions` key | environment | value |
| --- | --- | --- |
| `trace` | `MLS_TRACE` | `"off"`, `"messages"` or `"bodies"` (see [Tracing](#tracing)) |
| `traceFile` | `MLS_TRACE_FILE` | the file the trace is appended to |
| `requestDeadlineMs` | `MLS_REQUEST_DEADLINE_MS` | an integer of at least `1000` |

A relative `traceFile` is a path under the workspace root: the first of
`workspaceFolders`, else `rootUri`. With neither, or when the path climbs out
of the root, it is ignored. Use an absolute path to write elsewhere. A relative
`MLS_TRACE_FILE` is under the directory the server was started in.

```json
{ "initializationOptions": { "trace": "messages", "traceFile": "/home/me/mls.log" } }
```

A key the server does not know, and a value it cannot use, is ignored and noted
in the trace, whether it came from an option or from the environment.
Configuration never fails `initialize`, so a client written for a newer server
still gets a working one. `workspace/didChangeConfiguration` is ignored.

`requestDeadlineMs` is a tuning knob. It bounds how long the analysis worker may
spend on any one message while a request waits for it. Past it, the server
answers every waiting request with `ServerCancelled` and replaces the worker.
Loading a project does not count against it, and neither does a request held
for a rebuild. Its default is not part of the interface and may change.

## Tracing

The server speaks JSON-RPC on stdout, so it cannot log there. A trace is
appended to `traceFile`, else `MLS_TRACE_FILE`, else written to stderr, where
an editor collects a server's own output. With nothing configured, the default,
the server performs no logging.

Nothing is written until `initialize` has settled where the trace goes and
whether it is on. The lines from before it are kept and written to that
destination, without message bodies, or dropped when the trace is off. A
session that ends before `initialize` uses the environment alone. The trace
names the settings in effect and where each came from.

What a trace contains is a separate decision from whether it is on. A message
body is your source code: every `didOpen` carries a whole file and every
`didChange` carries what you just typed. Tracing is normally turned on to see
which requests arrived in what order, which does not need any of that, so the
`messages` level records only what each message *is* (direction, method, id,
size, timing) and no bodies.

| level | effect |
| --- | --- |
| `off` | no trace |
| `messages` | one line per message, and the server's own notes |
| `bodies` | also message bodies, truncated at 512 bytes each |

`MLS_TRACE` takes `off`, `messages` or `bodies`, and any other non-empty
value means `messages`, so `MLS_TRACE=1` works. The LSP trace setting names the same levels `off`, `messages` and
`verbose`.

The level at startup is the first of these that is given:

1. the `trace` option
2. `MLS_TRACE`
3. `initialize.trace`

So `MLS_TRACE` is not silenced by an editor that sends `trace: "off"` by
default, but the `trace` option does silence it. After startup, `$/setTrace`
moves the level for the rest of the session, whatever set it.

Use `bodies` only when you need the contents of a message, and be aware that
the log will then contain fragments of whatever you have open.

## How the compiler dependency is wired

`dep/mach` (id `mach`) provides the `mach.lang.*` compiler and retained frontend
surfaces this server binds to; `dep/std` (id `std`) provides `std.*`. Both are
declared as git dependencies in `mach.toml`: mach pinned to a release tag
(`tag/v5.12.0`) and std selected by the version range that mach builds with
(`^7.4`), and fetched by `mach dep pull .`. The committed gitlinks under `dep/`
are the pins; there is no lockfile. std follows the release mach's own CI,
because the server and the compiler it links share one std.

### Compiler compatibility

The server does not run `mach`. It contains the compiler, linked from exactly
one mach release, which `mls --version` and `serverInfo.mach` name. Every
project it opens is checked against that release.

A project states the compilers it builds with as `[project].mach` in its
`mach.toml`, and its dependencies may state their own. When the linked release
is outside any of those ranges, the project is not loaded. The server shows
why, naming each unmet range and the dependency chain that states it, as an
error on the root's `mach.toml` and in a message. A manifest without the key
loads with a warning there that gives the line to add. Both clear when the
manifest is fixed.

So a project that requires a newer mach than the server links needs a newer
server. A release that moves the linked mach says so in the changelog, naming
the old and new version. Moving to a new mach minor is at least an mls minor
release, an mls patch release moves only mach's patch, and a new mach major is
a new mls major.

## Architecture

| Module | Responsibility |
|---|---|
| `main` | entry point: `--version`, then the supervisor or, with `--worker`, the server loop |
| `supervisor` | the client-facing process: relays frames to the analysis worker, ends a stuck one, and replaces one that dies |
| `mirror` | the session state a replacement worker is replayed |
| `pending` | the requests the supervisor has seen and the worker still owes |
| `sideband` | the worker telling the supervisor when it loads or holds a request |
| `settings` | `initialize` options over the environment and the defaults |
| `version` | the server's version and the linked mach's, fixed at compile time |
| `server` | lifecycle state, reading loop, and the analysis-thread dispatch |
| `jobs` | bounded message queue feeding the single analysis thread |
| `transport` | LSP base-protocol framing over stdin/stdout |
| `json` | JSON-RPC reading over `std.data.json`, plus LSP payload assembly |
| `documents` | live URI/path/text/version/revision ownership plus fallback `FileId` |
| `diagnostics` | publish compiler snapshot diagnostics, with single-file fallback |
| `positions` | byte offset ⇄ LSP `(line, character)` (UTF-16 columns ⇄ bytes) and span text — the single conversion point, including across a stale snapshot's edits |
| `textdiff` | the windows where a snapshot's text and the client's buffer differ |
| `parked` | requests held until their root's snapshot catches up |
| `features` | offset → id → symbol query core over the resolve side tables |
| `project` | stable per-root compiler Sessions and retained Project snapshots, overlays, routing, fingerprints, module views, and invalidation |
| `language` | hover / definition / references / rename / documentSymbol / completion request bodies |
| `build` | the single-slot worker rebuilds run on, off the analysis thread |
| `notes` | what a project load says about the project itself, shown on `mach.toml` |
| `progress` | work-done progress for a cold load |
| `textedit` | applying an LSP change list to a document's text |
| `analysis` | resolving a positional request to the document view that answers it |
| `types` | helpers over sema's typing output |
| `render` | one spelling per LSP value: ranges, locations, symbol kinds |
| `signature` | signatureHelp |
| `folding` | folding ranges from the buffer's own parse |
| `selection` | selection ranges: a cursor expanding outward through the parse |
| `hints` | inlay hints naming arguments at a call |
| `tokens` | semantic tokens (`full`, `range` and `full/delta`), classified from resolved meaning |
| `actions` | code actions from the compiler's own fixes |
| `callhierarchy` | call hierarchy across modules |
| `workspace` | workspace/symbol |
| `trace` | the debug trace, held until `initialize` settles its destination |

## Deferred

- scope-aware completion (only the names in scope at the cursor): the
  resolver's scope chain is internal to the resolve pass and not exposed by
  its side tables;
- `utf-8` position encoding (#269).
