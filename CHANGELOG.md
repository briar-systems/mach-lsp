# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
