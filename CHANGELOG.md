# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.0] - 2026-08-23

Hover was rendering doc comments as one undifferentiated paragraph. Fixing that
turned up two more defects behind it, one of them upstream.

### Added
- hover: a declaration's doc components are named by the kind that owns them -
  `Parameters` for a function, `Fields` for a record, `Variants` for a union -
  and the return value gets its own line instead of a bullet among the inputs,
  where it read as though the function took an argument called `ret`. (#215)
- hover: a parameter, generic, or comptime parameter carries the description
  written for it. A field already did; the others did not, so a line an author
  wrote about an argument was reachable only by hovering the function and
  reading its list. `doc_span_of` refuses these deliberately - the alternative
  is dumping the enclosing function's whole doc block under a cursor on one
  argument - but nothing was reading the component line back out. The `[T]` and
  `$name` forms are stripped before matching, so a block spelled the way the
  spec asks for is still found. (#215)

### Fixed
- hover: a doc comment's bullet lists are no longer folded into the paragraph
  around them. Every line break became a space, which is right for wrapped prose
  and wrong for structure the author wrote deliberately: a five-item list
  arrived as one run-on paragraph with `- item` markers stranded mid-sentence.
  Indentation is what separates the two cases and was being stripped before the
  decision was made. A marker starts a line, a line indented further than the
  marker above it is that item wrapping, and a line back at the prose margin
  ends the list. Nesting is emitted relative to the list's own first marker, so
  a comment indenting its top level by four spaces stays a list rather than
  becoming a code block. (#213)
- hover: a carriage return is part of the line ending, not part of the line. A
  file saved with CRLF put a stray control byte into the middle of every wrapped
  line, and a blank comment line read as content rather than a paragraph break.
  Pre-existing and invisible on a Linux checkout; the Windows lane found it once
  a fixture was written the way an editor there would write it. (#213)

### Changed
- deps: advanced the vendored mach pin past briar-systems/mach#3072, a CRLF
  doc-block bug this repository found and reported. `is_space` in mach's doc
  parser did not count a carriage return, so `# ---` measured four characters
  wide in a CRLF file and the separator never matched - taking every parameter,
  field and return description out of hover on the platform where editors write
  CRLF, and out of `mach doc` and the docstring lint with it. Verified across
  all four combinations of disk and buffer line ending rather than taken from
  the closed issue. Session memory is flat: 237 MiB after a cold load, 238 MiB
  after 40 edits.

## [0.15.0] - 2026-08-22

The last epic closed. Analysis now survives a worker that crashes and one that
stops responding, and the two remaining hot-path costs turned out to be nothing
anyone had guessed at.

### Added
- runtime: a crashed analysis worker is replaced and the session replayed into
  it. Containment already survived a fault; it could not recover from one,
  because what the worker knew - which documents are open, what they now
  contain, what the client negotiated at `initialize` - died with it, and a
  client never re-sends any of that. The supervisor keeps its own mirror, built
  only from frames the client sent and never consulted to answer a request. The
  replay is written by the client pump rather than the thread that spawns the
  worker: a replay is more than a pipe holds and the diagnostics it provokes are
  more than the other pipe holds, so whoever writes it must not also be draining
  the answers. (#154)
- runtime: analysis that has stopped responding is ended. A compiler stuck in
  non-cooperative code never reaches a point where it could read a cancellation,
  so the request is answered `ServerCancelled` and the process is ended, which
  turns the hang into the crash the supervisor already recovers from. The
  deadline is two minutes and overridable - a cold load is seconds and a warm
  request is under a millisecond, so the failure mode of a short deadline is
  killing work that was about to succeed. Shutdown is bounded by the same
  watchdog, because a server that will not exit is one the user has to hunt
  down. (#156)
- progress: a cold project load reports `$/progress`, so seconds of silence look
  like work rather than a hang - which matters more now that the supervisor
  waits two minutes before calling analysis stuck. Only the first analysis of a
  root reports; a spinner on every keystroke teaches the reader to ignore the
  one that means something. A report whose worker dies is closed by the
  supervisor rather than left spinning. (#207)
- hover: content format is negotiated and bare expressions are typed. (#145)

### Fixed
- perf: documentSymbol no longer rescans the file once per span or re-parses the
  document once per request. `span_text` bounded every copy with
  `str_len(file.text)`, so a response cost the product of how many spans it
  named and how big the file was; `positions.Text` pairs a file with its length
  and `text_of` is its only constructor, so the two cannot be mismatched.
  `editor.parse` is unconditional, and `editor.update` drops a buffer's analysis
  whenever its text changes, so the cached tree is safe to reuse by
  construction. On a 2166-line module: 32.2 ms to 10.6 ms, and per-line cost is
  now flat rather than climbing with file size. (#203)
- trace: enabling `MLS_TRACE` no longer copies your source into `/tmp`. A
  message body is the user's code - every `didOpen` carries a whole file - and
  tracing is normally turned on to see which requests arrived in what order,
  which needs none of it. The default now records direction, method, id, size
  and timing; `MLS_TRACE=bodies` adds bodies back, capped at 512 bytes each, and
  `MLS_TRACE_FILE` moves the log off a shared path. (#207)
- trace: each protocol frame is recorded once, by the worker. Both halves were
  recording, so two processes appended to one file and raced for the offset;
  the loser's line was overwritten, which on Windows lost whole frames from the
  middle of the log. (#207)
- supervisor: a worker dying mid-forward no longer takes the session with it.
  Three defects on the same timing - a failed write treated as the end of the
  client connection, a failed replay marked as spent, and the crash reported
  before the replacement existed, so the client's reissue landed in a gap with
  nothing left to answer it. None reproduced serially; running the crash test 12
  to 20 ways concurrently failed 6 of 12 before and passes 20 of 20 after. (#154)
- hover: a bare `#` comment line keeps its paragraph break instead of folding
  into a space. (#145)

### Changed
- deps: advanced the vendored mach pin to `v4.25.0`. Session memory is flat -
  232 MiB after a cold load and 233 MiB after 40 edits - verified with a
  resident-memory series rather than the upstream byte assertion, which passed
  while RSS climbed.

## [0.14.0] - 2026-08-21

The last three upstream blockers landed, so the work they held up shipped
together.

### Added
- codeAction: quick fixes, built from the compiler's structured `fixes` rather
  than by parsing its prose. mach#3023 added spans and replacement text
  alongside `help`, which is what made this safe to build - reconstructing an
  edit from the compiler's English would couple the editor to diagnostic
  wording, so every rephrasing upstream broke a fix here silently. One `Fix`
  becomes one action carrying all its edits, since they apply together or not at
  all. (#165)
- ci: a windows-latest job. Windows path identity is lexical - drive roots, UNC
  authorities, native separators - and none of it is exercised by a Linux build.
  It failed three times before passing, each on something a Linux runner cannot
  see. (#157)

### Fixed
- paths: file URI identity is portable in both directions. `normalize_path`
  delegates to `std.types.path.clean` rather than a hand-rolled POSIX cleaner. A
  non-empty authority is a UNC host rather than a path segment, so
  `file://server/share` no longer decodes a remote path as local; a drive URI
  loses the leading slash the URI form adds; an encoded NUL is refused rather
  than truncating every later comparison. Outbound, URIs are built rather than
  concatenated: `C:\src\x` produced `file://C:\src\x`, where a client reads
  `C:` as the host. (#157)
- supervisor: the worker inherits this process's environment. It was spawned
  with a nil envp, so the child ran with none at all - `PATH`, locale,
  `MLS_TRACE` - which was invisible until a trace produced no output from it.
- deps: advanced the vendored mach pin to `v4.24.0` and mach-std to `v0.28.1`.

## [0.13.0] - 2026-08-21

### Added
- supervisor: a compiler front-end fault no longer takes the server down
  silently. The process the editor talks to owns the client's stdio and runs no
  compiler code; it spawns a second copy of this executable with `--worker` and
  relays frames. A fault becomes an ordinary child exit the parent survives and
  explains - the outstanding request is answered `-32603`, the person is told
  which signal killed it through `window/showMessage`, and the exit code is 3,
  distinct from a clean protocol exit (0) and a transport failure (1). The
  parent tracks which request is genuinely outstanding, clearing it as
  responses relay back, so a crash after a request was answered adds no second
  response. Restart with state replay is deliberately absent; that needs the
  parent to own document text, which is #154/#155's design. (#154, #156)
- server: every request is guaranteed a response. A handler that produced none -
  a latched allocation failure in its response buffer, a path that gave up
  without replying - left the client waiting forever; the dispatcher now detects
  that and answers `-32603`.

### Fixed
- deps: **advanced the vendored mach pin to `v4.22.0`**, which carries the
  retained-analysis leak fix (mach#3010, released in 4.20.1). Session memory is
  flat: 40 edits of one file on this repository hold at 228 MiB, against ~8 MiB
  per edit before this work and ~4.2 MiB after 4.20.0. (#159)

## [0.12.0] - 2026-08-21

Five new language features, cancellation, and the emit layer they all share.
Advertised capabilities go from 6 to 11.

### Added
- protocol: `$/cancelRequest` is honoured. A withdrawn request is answered
  RequestCancelled (-32800) rather than dropped, because a request still owes
  exactly one response. The cancellation is handled on the reading thread rather
  than queued - queued, it would be dequeued after the request it withdraws had
  already run. Cancelling work already in progress remains out of scope. (#153)
- documentHighlight, workspace/symbol, signatureHelp, inlayHint, and
  semanticTokens. Advertised capabilities go from 6 to 11. (#170, #167, #168,
  #169, #166)
- semanticTokens classifies from the resolved side tables rather than from
  spelling, which is the point in a language where an identifier may be a type,
  a function, a module alias, a parameter, a field, or a comptime value with
  nothing in how it is written to say which. 1389 tokens on `src/server.mach`.
- signatureHelp locates the enclosing call by scanning text rather than the Ast,
  because the request exists precisely while the argument list does not parse;
  it handles nesting, string literals, statement boundaries, and the type
  arguments of a generic call.
- inlayHint names literal arguments at multi-parameter calls. Mach requires an
  explicit type annotation on every binding, so the usual inferred-binding-type
  hint has nothing to show; suppression is the feature, taking 420 candidate
  hints on one file down to 76.
- workspace/symbol searches every loaded root, ranked so leading matches precede
  interior ones - `read_` should find `read_dir`, not `thread_spawn`.

### Changed
- analysis: request-to-document-view resolution moves to `mls.analysis`, so a
  feature binds to one contract instead of reaching into `language.mach`.
- render: `language.mach`'s remaining two-pass emitters are gone; `mls.render`
  is the only place that knows how an LSP value is spelled. (#171)

### Fixed
- hover: a record or union renders its declaration header rather than its whole
  body. `Server` hovered as fifteen lines of fields with its doc comment buried
  underneath; a binding with a multi-line initialiser did the same. (#145,
  first half)

### Known issues
- codeAction is blocked on briar-systems/mach#3023. A mach diagnostic's `help`
  is a sentence, not an edit - there is no span or replacement on the record - so
  building quick fixes today means pattern-matching the compiler's English,
  which breaks silently whenever upstream rephrases. (#165)
- a function-local `val` / `var` resolves in some buffers and not others, so
  hover, definition, references, and highlight silently decline on them in
  those files. (#181)

### Fixed
- completion: answers for the cursor rather than for the file. The server
  advertised `.` as a trigger character and then ignored the request position
  entirely, returning every top-level symbol whatever the cursor was on: `srv.`
  gave 178 items with **zero** `Server` fields among them, and a cursor mid-word
  at `STATUS_RUNNING` gave the same 178. A record or union receiver now offers
  its fields with their declared types, a module alias offers the target's public
  symbols, and anything else is filtered by the partial identifier. An
  unresolvable receiver offers nothing rather than the file's names. (#81, #82)
- definition / hover: `use` and `fwd` import paths navigate. Both answered null
  at every position on an import line — an import path is neither an expression
  nor a type and an import declaration has no name span, so the offset pivot
  missed it, while `decl_symbol` held the bound symbol the whole time. A module
  alias resolves to the module's file and hovers as `module <fqn>`; a symbol
  import resolves to its declaration. (#163)
- documentSymbol: reports record fields, union variants, function parameters, and
  generics as `children`, each with its declared type in `detail`. The outline was
  a flat list — 39 top-level symbols on `src/server.mach`, none with children. Now
  25 of 39 have them. (#164)

### Changed
- render: a new `mls.render` is the only place that knows how an LSP value is
  spelled - Range, Location, TextEdit, the response envelope, the standard empty
  replies. Responses were previously assembled by summing fragment lengths into
  an exact allocation and filling it in a second pass, which meant every
  data-dependent payload was traversed twice and each entry rendered twice, and a
  disagreement between the two passes under-filled the buffer so `str_len`
  truncated the frame at the resulting NUL - a valid `Content-Length` over a body
  stopping mid-token. `json.buf_len` / `buf_rewind` cover the one case that
  needed the sizing pass, a rename group only known to be empty once walked.
  `language.mach` 2364 → 1968 lines, two-pass emitters 4 → 0, manual
  `allocate[u8]` assembly 22 → 4. Incidentally faster: `documentSymbol`
  13 ms → 5 ms, `completion` 14 ms → 0.2 ms. (#171)
- completion: an empty result is the same `CompletionList` shape as a populated
  one rather than a bare array, so the method has one response type.

## [0.11.0] - 2026-08-20

The server was unusably slow on any project that vendors the compiler, reported
diagnostics that depended on what the user had clicked, and blocked the client
mid-write while it worked. This release is those three, plus the architecture
they needed.

### Fixed
- perf: source fingerprints are stat-only. `file_fingerprint` read and FNV-hashed
  every module's full source on every snapshot build and every 250 ms scan, and
  its hash loop re-evaluated `str_len` as the loop condition, making the hash
  quadratic in file size. On this repository (222 modules) that put 225 s of the
  server's own bookkeeping in front of a 7.4 s compiler analysis, and repeated all
  of it on every later request. `textDocument/definition` cold 231 s → 7.4 s, warm
  222 s → 0.2 ms, after an edit → 181 ms. (#95)
- diagnostics: publishing drives the analysis instead of reporting whichever
  snapshot a previous request left behind. A file with a type error opened clean
  and only started reporting once an unrelated feature request built the project,
  then went quiet again when that snapshot was invalidated. Editor output now
  matches `mach build` from `didOpen`, and a hover no longer changes it.
  Republishing is scoped to the root that changed, rather than every open buffer
  in every open project. (#142)
- server: `exit` terminates the process even when the client leaves stdin open,
  which the spec entitles it to do.
- transport: distinguishes clean EOF from malformed/error input, caps LSP
  headers at 8 KiB and bodies at 16 MiB with overflow-safe `Content-Length`
  parsing, and surfaces response-write failures to the server loop. (#149)
- json / transport: the decimal formatters wrote `('0' + (v % 10))::u8`, mixing
  a `u8` char literal into `usize` / `i64` arithmetic. Sema types that
  expression at the wider operand and lowering typed it at the literal's own
  width, so mach 4.18 refuses it in its IR verifier - the compiler's own defect
  (mach#2949), but the source was relying on the two passes disagreeing. The
  width the arithmetic happens at is now stated: `'0'::usize` / `'0'::i64`.
- hover: a seeded vector type name (`res.SYM_VECTOR`) rendered as `symbol`
  rather than `type`; `kind_label` enumerated symbol kinds 0..11 and fell
  through on 12.

### Added
- server: analysis runs on a worker thread. The reading thread owns stdin and
  nothing else, so a cold analysis no longer stops the server from seeing the
  cancellation, edit, or shutdown behind it - and no longer blocks the client
  mid-write when a `didChange` fills the pipe, which is the stall an editor reads
  as a dead server. Flooding the server with 60 edits during a cold load went from
  18.6 s of blocked writes (worst single write 7.5 s) to 0.00 s. (#143)
- server: document revisions superseded by queued input are coalesced, so a burst
  of keystrokes costs one analysis of the newest text rather than one per
  revision. Deferring is never dropping: the debt is paid when the queue drains,
  whatever the last message was. The same 60-edit burst: 19.0 s → 0.9 s. (#155)
- jobs: bounded single-consumer message queue, futex-blocking so an idle server
  costs no CPU. The bound is memory as much as latency, since the queued bodies
  are whole documents.

### Changed
- project: replaced the shared-session, manually re-resolved graph with one
  stable compiler Session and retained `analyze_project` snapshot per root.
  Open document text is mirrored as filesystem overlays with explicit input and
  snapshot revisions; resolve, sema, generics, diagnostics, target/profile,
  `$project`, `$bin`, aliases, and dependency exports now come from compiler-owned
  ModuleEntries. Source content fingerprints, acknowledged watcher registration,
  `didSave`, and failed-snapshot retry keep snapshots current. (#141)
- documents: now own URI, canonical path, live text, LSP version, and a monotonic
  mutation revision independently of the editor fallback. Published diagnostics
  carry the matching document version.
- json: JSON-RPC messages are parsed with `std.data.json` instead of scanning the
  raw body for a quoted key. The scanner matched a key ANYWHERE in the document,
  including inside an opened file's own source text - which is exactly what a
  `didOpen` payload carries - and correct dispatch relied on clients ordering
  `method` before `params`, which JSON does not guarantee. Request ids now keep
  their wire type, malformed bodies get a spec parse error, notifications are no
  longer answered, and string escaping is the std emitter's. Messages parse into
  a per-message arena. (#153)
- project: parsed `mach.toml` tables are torn down. Both reads - the vendoring
  probe and the document-overlay source-directory lookup - run on the snapshot
  rebuild path and leaked their whole table on every rebuild, because
  `std.data.toml` had no teardown to call. mach-std#474 adds one; `get_str` and
  `table_key` borrow out of the table, so it is released at scope exit rather
  than at the last read. (#159)
- deps: **advanced the vendored mach pin `v4.7.1` → `v4.20.0`** and mach-std
  `0.22.0` → `0.27.0`, and returned `[dep.mach]` to `branch/main`. The server
  analyzes buffers with the vendored compiler frontend, so the pin *is* the
  language version the editor understands: frozen at 4.7.1 it reported everything
  added since - `#[packed]`, a declaration-scope `$if` measuring a layout,
  `$size_of` / `$align_of` / `$length_of` folding in a comptime gate, the
  `#[handle]` / `#[op]` target-owned type and operation declarations, riscv32 and
  the `ilp32` ABI family - as an error against source the installed compiler
  accepts. Tracking `branch/dev` was a temporary measure while the
  retained-analysis frontend API (mach#2997) was unreleased. (#141, #159)
- deps: repairs the frontend API drift the advance surfaced. `comptime.init`
  takes the target's `vector_bits` between `pointer_width` and the compiler
  name, and the target's operation / type-constructor table now reaches the
  front end as data on the comptime context (mach#2888), so a buffer resolving
  under a project seeds `set_target_defs` from its own target the way the
  compiler's own driver does. Without it a `#[handle]` or `#[op]` declaration
  resolves against no definitions at all.
- manifest: `linux-riscv64` moves from `abi = "lp64"` to `abi = "lp64d"`.
  mach#2777 made `lp64` mean what it says - soft float, every float in an
  integer register - where it had always emitted hard-float code. The old
  spelling still builds and would have silently changed the emitted calls.

### Known issues
- resident memory grows ~4.2 MiB per analysis, without bound. The tracked leak is
  61 KiB and ~887 unfreed allocations per analysis; because mach's page allocator
  is one mmap per allocation and the residue is overwhelmingly 2-16 byte strings,
  each leaked allocation pins a 4 KiB page, so RSS grows ~67x the byte count.
  It is entirely inside the compiler's `begin_build` - before any module is
  loaded, resolved, or type-checked - and reproduces with no LSP code involved.
  briar-systems/mach#3001 fixed the manifest-reload half of the original leak;
  briar-systems/mach#3012 tracks the rest. The LSP side is clean: the server adds
  no measurable growth over the bare compiler cycle. Consumer tracking is #159.

## [0.10.0] - 2026-08-07

### Added
- diagnostics: a diagnostic's `note` and `help` lines now ride the published
  message, and its secondary `related` locations become LSP
  `relatedInformation`. The compiler has always attached all three - `mach
  build` renders them - but the editor dropped them, throwing away the half of
  a mach diagnostic that says what to do about it. A secondary location
  resolves its own URI, so a "previous definition here" pointing into a
  dependency module is a link the client can follow. (#135)
- json: `Buf`, an append-only growable JSON sink. The fixed-shape
  sum-the-lengths-then-append pattern cannot express a payload whose shape is
  data-dependent (a diagnostic's relatedInformation array); every response of
  that kind is built through `Buf` instead.

### Changed
- deps: **advanced the vendored mach pin `v3.6.1` → `v4.7.1`** and mach-std
  `0.20.x` → `0.22.0`. The server analyzes buffers with the vendored compiler
  frontend, so the pin *is* the language version the editor understands: frozen
  at 3.6.1 it reported everything added since - `#[embed]`, the comptime type
  predicates and `$type_name`, `#[naked]` / `#[noinline]`, the unified
  inline-asm grammar, the `platform` target tag - as an error against source
  the installed compiler accepts. Repairs the frontend API drift the advance
  surfaces: the `std.filesystem` rename to `read_string` / `metadata` /
  `write_bytes`, `pointer_width` moving from `RegMachine` to the ISA vtable,
  `build_project_union` returning `outcome.Fail`, and `intern_instance` taking
  the template's nominal TypeId. (#135)
- project: manifest / lockfile staleness is checked in unix nanoseconds rather
  than seconds. The check is an equality test, and a save followed immediately
  by a request lands inside the same second.
- deps: Advanced the vendored mach pin (`8045f941` → `da9b0896`, v3.5.1 → v3.6.1) to the then-current release tip; the only notable delta is the retired x86_64-darwin platform (mach#2104). (#133)
- manifest: Re-touched to RFC-exact totality per the V2 manifest spec (mach#1964/mach#1979).

### Fixed
- deps: Bumped the vendored mach pin (`5b3eef8d` → `8045f941`, v3.5.1) past the required `simd` profile key (mach#1965/mach#2013) and the #1971 flag-day strict-root manifest parse, so the server loads current `mach.toml` manifests instead of rejecting them (`unknown key 'simd'`). (#131)

## [0.9.0] - 2026-07-07

### Changed
- manifest: Migrated manifest layout to comply with the V2 manifest spec.
- dependencies: Changed path-based dependencies to git dependencies pointing to GitHub repositories.
