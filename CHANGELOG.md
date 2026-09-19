# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.2] - 2026-09-19

One completion fix on the 1.2 surface, the third of @Angluca's reports about a
`.` on a receiver the resolver could not reach while the buffer is ahead of the
snapshot. No contract change: same asset set, CLI and options as 1.2.0.

**The linked mach is unchanged at v5.8.0**, and std at v4.0.0.

### Fixed
- fix(#336): completion after a `.` on a chained receiver (`value.field.`) now
  offers the members of the last field's type however many hops it has, while
  the buffer is ahead of the snapshot. The isolated resolver handled one hop at
  a time with a case per receiver shape, and none walked from a record to a
  field's declared type, so a chain into an imported record came back empty
  past the first hop. Every receiver is now resolved by one walk over its
  chain: the head by what it names (a module alias, a value's written type, or
  a tag type name) and each further segment as a field whose declared type
  resolves to the next record, union or tag, through the buffer's `use`s or the
  declaring module's own resolution, following `fwd` re-exports at every hop.
  The former receiver cases are the first hop of that walk rather than siblings
  of it. The third of @Angluca's completion reports (#332, #334, #336).

## [1.2.1] - 2026-09-19

Two completion fixes on the 1.2 surface, both reported by @Angluca and both
about a `.` on a receiver the resolver could not reach while the buffer is ahead
of the snapshot. No contract change: same asset set, CLI and options as 1.2.0.

**The linked mach is unchanged at v5.8.0**, and std at v4.0.0.

### Fixed
- fix(#332): completion after a `.` no longer comes back empty when the
  receiver's type is reached through a `fwd` re-export. While the buffer is
  ahead of the snapshot the receiver's type is resolved against the snapshot
  through the file's `use` and `fwd` text, and the resolver stopped at the
  re-exporting module, which forwards the type without declaring it, so the
  members were empty. It now follows the re-export to the declaring module,
  through as many `fwd` hops as the language allows, for both a rec and a uni.
  Reported by @Angluca.
- fix(#334): completion after a tag type name's `.` now offers the tag's case
  selectors. A tag's cases are named through the type (`T.c`), so `T.` is a
  type-name receiver, a kind the resolver did not have, and it offered nothing
  same-file or imported. It is now the fourth receiver kind, resolved same-file
  from the buffer and otherwise against the snapshot through the file's `use`
  declarations, following `fwd` re-exports like the imported-value kind. The
  other half of @Angluca's #332 report.

## [1.2.0] - 2026-09-19

The server negotiates the LSP position encoding, and the linked mach moves to
v5.8.0. No contract change beyond the additive positionEncoding capability:
same asset set, CLI and options as 1.1.1.

**The linked mach moves from v5.5.1 to v5.8.0**, and std is unchanged at v4.0.0.

### Added
- feat(#269): the server negotiates the LSP position encoding. utf-16 is the
  base-protocol default and stays the promise for any client that advertises
  none, so existing behaviour is unchanged. A client that offers `utf-8` in
  `general.positionEncodings` gets it (its columns are byte offsets, matching
  the compiler's own with no conversion), or `utf-32` (codepoint columns). The
  chosen encoding is echoed back in `ServerCapabilities.positionEncoding`. Every
  column conversion runs through the single point in `positions`, so the
  negotiated units apply uniformly to inbound positions and outbound ranges.

### Changed
- chore: the linked mach moves from v5.5.1 to v5.8.0. The driver's Project now
  holds its modules in stable storage (a `handle.StableChunks`), so the snapshot
  and lookup paths move to the driver's `module_count` and `module_at`
  accessors. No behaviour change, and v5.7.0's backend-refusal reshaping
  (mach#3656) does not reach this frontend.

### Test
- test(#325): the completion tests that assert the isolated answer while the
  buffer is ahead of the snapshot now hold the off-thread rebuild through a
  lock-file turnstile the harness releases, so that state is controlled rather
  than raced. The `MLS_TEST_REBUILD_GATE` environment variable driving it exists
  for the protocol harness only and is inert when unset.

## [1.1.1] - 2026-09-19

A completion fix on a 1.1 surface. No contract change: same asset set, CLI,
options and behaviour as 1.1.0.

**The linked mach is unchanged at v5.5.1**, and std at v4.0.0.

### Fixed
- fix(#321): completion after a `.` no longer comes back empty in two cases. A
  module alias used as a type qualifier (`var v: vector.`) now offers the
  module's symbols: the receiver before the dot is identified by its own text
  rather than through an expression node, so a type position resolves like an
  expression one. And a value whose record or union type is imported
  (`var v: vector.Vector[u8]; v.`) now offers that type's fields while the buffer
  is ahead of the snapshot: the value's written type annotation is read from the
  buffer and its record resolved against the snapshot through the file's `use`
  declarations, the third receiver kind after a same-file field and a module
  alias. Reported by @Angluca.

## [1.1.0] - 2026-09-18

The #297 crash fix becomes structural. No LSP contract change: same asset set,
CLI, options and behaviour as 1.0.2.

**The linked mach moves from v5.4.0 to v5.5.1**, and std is unchanged at v4.0.0.

### Changed
- refactor(#313): the completion handler no longer refetches the buffer's
  `SourceFile` after an analysis. The dangling-pointer crash behind #297 was that
  loading a dependency's sources grew the session source map and moved its
  backing array, leaving a stale pointer; 1.0.1 refetched the pointer to work
  around it. mach v5.5.1 (mach#3633) backs the source map with stable storage, so
  a `SourceFile` pointer survives the map growing and the refetch is gone. The
  dependency-alias regression test stays as the guard.

## [1.0.2] - 2026-09-18

A latency fix on a 1.0 surface. No contract change: same asset set, CLI,
options and behaviour as 1.0.1.

**The linked mach is unchanged at v5.4.0**, and std at v4.0.0.

### Changed
- perf(#252): the first edit after an idle pause is no longer a cold build. A
  root serves from one session and rebuilds into a spare, and that spare's first
  build is cold, so the first edit after a load rebuilt the whole project from
  scratch even though the load had just done that work. While the analysis
  thread is idle it now warms the cold spare with the snapshot's own input, so
  the edit that follows rebuilds warm. Measured against the mls project itself
  (~28 s cold), the first edit after a 45 s pause falls from ~28.5 s to ~1.6 s.
  A pause too short to finish the warm-up shortens proportionally, and with no
  pause the figure is unchanged (~27.7 s against ~27.6 s): the warm-up holds the
  single build slot, so an edit that arrives before any idle waits on that
  in-flight build exactly as it would have waited on its own cold rebuild, with
  no duplicated work and no second live snapshot beyond the one a rebuild always
  makes.

## [1.0.1] - 2026-09-18

A crash fix on a surface 1.0 froze. No contract change: same asset set, CLI,
options and behaviour as 1.0.0.

**The linked mach is unchanged at v5.4.0**, and std at v4.0.0.

### Fixed
- fix(#297): completion no longer crashes the analysis worker after a
  dependency-module alias's `.` while the buffer is ahead of the snapshot.
  The isolated editor analysis loads the aliased dependency's own sources,
  which grows the session source map and moves its backing array; the handler
  then read the buffer through the now-stale pointer, faulting on macOS where
  the freed page is unmapped. A local module never triggered it because its
  source is already resident. The pointer is refetched after the analysis.

## [1.0.0] - 2026-09-18

The stability promise (#270). Same code as 0.21.1: this release changes the
version and nothing else. mach-zed 0.9.0 runs on it clean.

**The linked mach is v5.4.0**, and std v4.0.0.

From this release on, these do not change without a major version: the
release asset names `mls-<version>-<platform>.tar.gz` and `.zip` for the five
shipped platforms, with `SHA256SUMS` over every other asset; `RELEASES.json`,
one object mapping each mls version to the mach it links, newest first,
listing only versions whose release carries the full asset set; the CLI, `mls`
and `mls --version` printing `mls <version> (mach <compiler version>)`; the
`initializationOptions` keys `trace`, `traceFile` and `requestDeadlineMs`
with their environment variables `MLS_TRACE`, `MLS_TRACE_FILE` and
`MLS_REQUEST_DEADLINE_MS`, the precedence option, environment, default, and
an unknown key or unusable value ignored with a note rather than failing
`initialize`; and a relative `traceFile` resolving under the workspace root
only, never outside it. The LSP capabilities and behaviour the README lists
are the surface this promise covers. Positions are UTF-16 (#269).

## [0.21.1] - 2026-09-18

Fixes found by mach-zed's re-run against 0.21.0 and by an outside report,
all on surfaces 1.0 freezes (#270).

**The linked mach is unchanged at v5.4.0**, and std at v4.0.0.

### Fixed
- fix(release): `RELEASES.json` lists only versions whose release carries the
  full asset set, so every entry can be installed (#299). Tags without a
  release, and releases without their archives, are left out. The release
  fails if a listed version lacks its assets.
- fix(settings): a relative `traceFile` that resolves outside the workspace
  root, such as `../escape.log`, is ignored with a note instead of written
  beside the project (#300).
- fix(completion): completion after a module alias's `.` lists the module's
  public names while the buffer is ahead of the project snapshot, which is
  every keystroke while typing (#297). It used to be empty until a rebuild
  landed, because the isolated single-file analysis that answers then cannot
  resolve a `use`. The alias's module is now found in the last snapshot by
  the name the `use` writes. A `use` of a module that snapshot has not seen
  still offers nothing until the rebuild lands.

## [0.21.0] - 2026-09-17

Fixes to the 0.20.0 surface found by the mach-zed evaluation, and the
`RELEASES.json` asset editor extensions use to choose an mls (#270).

**The linked mach is unchanged at v5.4.0**, and std at v4.0.0.

### Added
- feat(settings): a relative `traceFile` is resolved against the workspace
  root, the first of `workspaceFolders`, else `rootUri` (#287). With neither it
  is ignored with a note, as before.
- feat(release): each release publishes `RELEASES.json`, mapping every mls
  version to the mach version it links, so an editor extension can pick the
  newest mls a project's `[project].mach` range accepts (#288). It is generated
  from the tags when the release is cut, covered by `SHA256SUMS`, and checked
  against the release's own binary. Its format is frozen at 1.0.

### Fixed
- fix(rename): a rename that would break the project, or change what it means,
  is refused with `RequestFailed` rather than returned as edits (#286). Each
  file the rename touches is parsed again and must produce the same tree. That
  refuses names that are not identifiers, and names the grammar reads
  differently where they would stand, while contextual keywords the grammar
  accepts in every such place stay renameable. A new name already bound in a
  touched module, a built-in type, or a local in a top-level declaration the
  rename touches is refused too, as is a field name the record already has.
  Renaming a dependency's symbol is now an error, from rename and from
  prepareRename, rather than an empty edit or null.
- fix(supervisor): `requestDeadlineMs` no longer ends the server when a project
  load takes longer than the deadline (#284). The deadline now bounds the time
  the worker spends on any one message while a request waits for it. A project
  load, and a request held for a rebuild, do not count against it. The worker
  tells the supervisor when it takes up a message, loads, or holds a request.
  Responses the client sends to the server's own requests, such as progress
  token creation, are no longer taken for requests.
  That mistake left a request marked outstanding that no reply would ever
  close, so every replacement worker was ended within a fraction of a second
  until the server exited. When the worker dies, every request it still owed is
  now answered, including held requests and requests with string ids.
- fix(trace): nothing is traced before `initialize` has settled where the trace
  goes (#285). The first lines of a session, including the `initialize` body at
  the `bodies` level, used to go to the environment's file even when the
  `trace` option turned tracing off or `traceFile` named another file. Those
  lines are now held in memory, without bodies. They are written to the chosen
  destination, or dropped when tracing is off.
- docs: the README names the pinned mach as v5.4.0, and says the worker's
  memory figures are resident plus swap, with the scenario each one measures
  (#287).
- test: the check that a request held for a rebuild outlives the deadline no
  longer depends on how fast the machine rebuilds (#294).
- fix(trace): with no file configured, the trace goes to stderr rather than the
  shared `/tmp/mach-lsp.log` (#285).
- fix(settings): `MLS_TRACE=off` turns tracing off instead of on. An unusable
  `MLS_TRACE_FILE` or `MLS_REQUEST_DEADLINE_MS` is noted in the trace the way an
  unusable option is. `MLS_REQUEST_DEADLINE_MS` now has the same minimum as the
  option. The trace names the effective settings and where each came from
  (#285, #287).

## [0.20.0] - 2026-09-17

Configuration at `initialize`, project-level diagnostics on `mach.toml`,
cross-module references and rename, and the move to mach's compiler ranges.
This is the release the 1.0 surface ships in first (#270).

**The linked mach moves from v5.2.1 to v5.4.0**, and std from v3.2.0 to v4.0.0.
Projects are now checked against mach 5.4.0: one whose `[project].mach`, or a
dependency's, excludes 5.4.0 is not loaded, and the reason is shown on its
`mach.toml`. Building mls itself needs mach 5.3 or later.

### Added
- feat(project): what a project load says about the project itself is shown on
  the root's `mach.toml` (#266). mach 5.3 records some warnings against no source
  file, such as the one for a manifest without `[project].mach`, and refuses a
  load when the compiler is outside a range the closure states. Neither reached
  the editor before. The server now publishes them as diagnostics on
  `mach.toml`, and publishes the list again whenever it changes. A fixed
  manifest therefore loses its complaint, whether the fix arrives as a watched
  file change or is found by the next request. A root whose load failed is
  retried as soon as a change is reported, and a rebuild for a loaded one starts
  then too, rather than at the next message.
- feat(settings): a configuration surface at `initialize` (#264). The
  `initializationOptions` keys `trace`, `traceFile` and `requestDeadlineMs`
  take precedence over `MLS_TRACE`, `MLS_TRACE_FILE` and
  `MLS_REQUEST_DEADLINE_MS`. Unknown keys and unusable values are noted in the
  trace and ignored. `initialize.trace` sets the level when neither the option
  nor the environment does, and `$/setTrace` moves it afterwards. The settings
  carry over to a replaced worker. See the README's Configuration section.

### Changed
- docs(readme): the status and deferred lists match what the server
  advertises: incremental sync, workspace symbols, member completion and the
  other features added since they were written (#280).
- docs(readme): the known limits are measured again on mach 5.4.0 and std 4.0.0
  (#280): about 26 s to the first semantic answer and to the first rebuild,
  1.2-1.4 s for later rebuilds, and 870 MiB to 1.35 GiB of worker memory with a
  1.7 GiB peak.
- chore(dep): the server links mach 5.4.0, built against std 4.0.0, the std
  mach's own CI proves (#266, #280). 5.4.0 carries the fix for mach#3536,
  without which a rebuild with std 4.0 took 16-25 s on this repository. std 4.0 passes descriptors as pointer-width
  handles, so the transport, the supervisor's pipes and the trace log hold
  handles now. Projects are checked against the linked mach's version: one whose
  `[project].mach` excludes it is not loaded. The README's Compiler
  compatibility section describes this.
- chore(project): mls states its own compiler range, `mach = "^5.3"`, and
  builds only with mach 5.3 or later (#266).
- chore(license): copyright is attributed to Briar Systems LLC, 2025-2026
  (#277). The MIT terms are unchanged.
- docs: the README states the first-load time, rebuild times and resident
  memory as known limits, measured on this repository (#268).
- feat(version): `mls --version` prints `mls <version> (mach <version>)`, and
  `initialize` reports the compiler version as `serverInfo.mach`.
  `serverInfo.version` is unchanged.
- docs: the README documents the public command line, `mls` and
  `mls --version`, and marks `mls --worker` private (#265). `--worker` is the
  supervisor's re-launch of itself, with no stability promise.

### Fixed
- fix(features): `tag` declarations have a name span (#249). The outline
  listed no tag, and definition, references, rename and highlight could not
  start from a tag's name. The outline also lists a tag's cases as enum members,
  with a payload type as the detail.
- fix(references): references and rename reach every module that imports the
  symbol (#246). The walk identified its target by DeclId, which a `use`d
  binding does not carry, so asked from an importer it never left the open
  buffer, and a rename rewrote one file of a cross-module symbol. The target is
  now the defining symbol. Every module contributes each binding that denotes
  it, by declaration or by canonical name and kind. Rename keeps an import
  alias's spelling and rewrites its path.
- fix(project): on windows, an open document that no module imports is
  analyzed (#273). Open documents join the load when they lie under the source
  directory, and that test only accepted `/`, while windows paths are spelled
  with `\`. Such a document was never loaded, and definition, workspace symbols
  and references all skipped it.

## [0.19.0] - 2026-09-16

Navigation, answers while the project rebuilds, off-thread rebuilds, and the
first release with prebuilt binaries. Built against mach v5.2.1 and std v3.2.0.

### Added
- feat(cd): releases publish a prebuilt release-profile `mls` for
  `x86_64-linux`, `aarch64-linux`, `x86_64-windows`, `aarch64-darwin` and
  `x86_64-darwin` (#260).
  - Assets are `mls-<version>-<platform>.tar.gz` (`.zip` on windows) and
    `SHA256SUMS`, named for editor extensions (see the README).
  - Each binary is built and run on its own host, and must report the tag's
    version.
  - The release is drafted with every asset, checked, and only then made public.
  - A dispatch rehearses the whole path and stops at a draft.
- build: the windows server is `mls.exe` (`out = "bin/mls{artifact.suffix}"`);
  it was written as `mls`, which a client launching `mls` cannot run.
- feat(server): `mls --version` prints `mls <version>`, and `initialize`
  reports it as `serverInfo.version`, both from `[project].version`.
- feat(navigation): `textDocument/typeDefinition` goes to the declaring site of
  an expression's type. `RecordSite` admits `rec` / `uni` only, which is right
  for the field features but would answer null for a `tag` - the type of every
  `opt` and `res` in the language - so the nominal back-link generalises to
  `project.TypeSite`. The cursor tries four candidates and takes the first that
  resolves to a declaration rather than the first that types, because a callee's
  own type is a function type and committing to it answers null on `make` in
  `ret make();`. A cursor on a function parameter's name reads the parameter's
  written type, since a parameter binds no declaration of its own.
- feat(navigation): call hierarchy - `textDocument/prepareCallHierarchy`,
  `callHierarchy/incomingCalls` and `callHierarchy/outgoingCalls`. An item is
  addressed by `uri` plus `selectionRange.start` and re-derived on every
  request rather than held in a server-side handle table, so nothing is
  retained between requests and no lifetime rules are owed. Item resolution
  reaches the project's module snapshot through the new
  `analysis.analyze_uri`, because an item's file is usually one the editor
  never opened. A call through a `fun` value is reported rather than omitted,
  as SymbolKind.Variable carrying a `detail` that says the target is not
  statically known; a callee that resolves to no symbol at all is anchored on
  the call site itself. A buffer belonging to no project reports its own
  callers rather than none, and what a callee is is decided separately from
  where it is declared, so a call whose declaring module was never loaded keeps
  its function kind instead of claiming its target is unknown.

### Changed
- ci: the workflow follows the family CI contract (briar-systems/mach#3447). It
  runs on pull requests and dispatch only, calls the shared `mach-lib.yml`
  pipeline with windows as a second light leg (#157), runs the protocol suite
  per host from `.github/ci/verify.sh` against the verified seed compiler, and
  ends in a `gate` job. The release workflow calls it with `heavy: all`.
- build: the mach pin advances to v5.2.1 and std to v3.2.0, the family seed.
  The server builds and passes its suites against std 3.2.0 unchanged.
- refactor(render): the LSP SymbolKind table for a declaration kind now has one
  spelling, `render.symbol_kind`, which is that module's stated purpose.
  documentSymbol and the call hierarchy both name declarations and each had its
  own copy; they disagreed about `tag`. As a result a `uni` and a `tag` are both
  SymbolKind.Enum and a `test` block is SymbolKind.Function. `tag` declarations
  still never reach the outline, because `features.decl_name_span` has no arm
  for them - see #249.
- project: a root that already has a snapshot rebuilds on a worker thread
  instead of on the analysis thread. Each root owns two sessions and ping-pongs
  between them: one serves every request while the other is built into, and the
  finished snapshot is swapped in at a message boundary, where no request holds
  a pointer into the outgoing session. The idle session keeps its query cache,
  which is what keeps a rebuild a fraction of a cold build - building into a
  fresh session each time measured ~34s against this repo where the retained
  one measures ~7s. A root with no snapshot has nothing to serve, so its first
  build still runs inline, and a failed rebuild leaves the previous snapshot
  serving rather than dropping it.
- project: a document's project root is resolved from the normalized path, the
  same spelling documents themselves carry. `project_root_for` keeps whatever
  spelling it is handed, so deriving the root from the raw URI path gave the
  same directory two names on windows - `C:/x/alpha` against `C:\x\alpha` -
  and every comparison between a root and a document's own root silently said
  no. That decides whether a buffer is mirrored into a root at all, so a root's
  first build captured no open buffer and its snapshot revision never left zero.
- project: snapshot staleness is tested per document rather than per root. A
  root-wide test called the root stale whenever any covered buffer had moved,
  including when the move was a rebuild that FAILED - the attempt is recorded,
  the snapshot revision is not - leaving the root permanently stale with nothing
  left to schedule and every cross-module feature dead until an unrelated edit
  happened to succeed. Keyed on the document, a failed rebuild leaves every
  buffer the last good snapshot still describes exactly where it was.
- diagnostics: the interim publish for a document governed by a loaded project
  runs to parse, not sema. It is an answer a rebuild is already on its way to
  replace, and running it to sema dragged the whole import closure through the
  analysis thread - the exact cost moving the rebuild off it was meant to
  remove. Where no project governs the document, that analysis is still the
  authority and still runs to sema.
- jobs: the analysis thread's queue multiplexes client messages with an
  internal signal, so a rebuild finishing while the client is idle still
  reaches the editor instead of waiting for the next keystroke.
- test: the protocol suite asserts the healthy-project latency of every
  syntax-only feature, not just the standalone path. The previous assertion
  broke the manifest before timing `documentSymbol`, so it measured the one
  path that was never slow and a 40x regression shipped in 0.18.0 unnoticed.
  The new bound is a ratio between the two paths measured in the same run, so
  machine load cancels, and the fixture carries a real dependency because a
  reload costs what its dependencies cost and a dependency-free project cannot
  show one.

### Fixed
- fix(supervisor): the server starts on aarch64-linux (#259). The worker was
  spawned with stderr passed as descriptor 2, which std redirects with `dup3`
  on aarch64 and riscv64, and `dup3(2, 2)` fails, so the worker died before
  exec. It now passes -1, std's spelling of inherit. Found by the new heavy
  aarch64 CI leg.
- fix(project): a buffer whose snapshot is rebuilding is answered instead of
  ignored (#251). `document_view` refused a snapshot older than the buffer, and
  nothing fell back, so every semantic request on an edited buffer answered null
  until the rebuild landed: ~33s after the first edit on this repo, ~6.5s after
  any later one. The stale snapshot now answers through `textdiff`, a Myers line
  diff narrowed to bytes. A position touching an edit answers nothing, and names
  and references refuse a span whose own bytes changed. One window from the
  common prefix to the common suffix was measured first and rejected: an import
  added at the top and used below blinds a median 53% of a file's identifiers
  with one window, and 0.47% with the list. References, rename, prepareRename
  and code actions are held until the root is current, then re-dispatched in
  arrival order. A cancel answers a held request at once, and didClose and
  shutdown answer theirs with ContentModified. A rename that would leave an
  occurrence unmapped answers ContentModified instead of half an edit. They
  also now require every buffer of the root to be current, not just their own,
  since a sibling's stale picture made a rename miss or misplace its edits.
  Diagnostics keep the buffer's parse errors when it has any, and otherwise
  carry the snapshot's semantic diagnostics that avoid the edits. Clients that
  advertise `refreshSupport` are asked to refresh semantic tokens and inlay
  hints after a swap that replaced stale answers.
- fix(tokens): a semantic token dropped for spanning lines no longer leaves a
  leading comma when it is the first one.
- fix(project): a source file written while a build ran is rebuilt rather than
  recorded as already seen (#254). A build fingerprinted its modules after
  analyzing them, so a write landing in between left a snapshot of the old
  bytes beside a fingerprint of the new ones, and no scan would find it. A
  fingerprint is now kept only when the file still holds the analyzed bytes.
- a `use`d symbol carries its referent's `origin` but no `DeclId` of its own,
  so a decl-keyed cross-module identity test reports that no module importing a
  function calls it. `features.symbol_denotes` adds the interned canonical name
  and the declaring kind, which those bindings do carry, and the call hierarchy
  walk uses it. `references` and `rename` still identify by `DeclId` alone and
  still have the gap; see #246, kept separate because it widens what `rename`
  rewrites.
- perf(analysis): a syntax-only request no longer reloads the project through
  the editor session. `editor.analyze` tore the project down on every call, so
  `documentSymbol` against a healthy project paid a full reload: against this
  repo, a 1586.9ms median versus 34.8ms under a manifest that cannot load. The
  cause was mach#3431, and the pin advances to v5.1.0 to carry the fix, which
  puts the same measurement at 1.00x. std advances to v2.2.0 alongside it.

## [0.18.0] - 2026-09-13

Mach 5.0 migration. The server now builds against the mach v5.0.0 compiler and
std 2.0.0, follows the 5.0 driver and type-table contracts, and drops the
lockfile in favour of the committed dependency gitlinks.

### Changed
- build: `mach.toml` follows the 5.0 manifest rules (complete profiles, one
  default target and profile, `[dep.std]` realized at `dep/std`, pins on
  `tag/v5.0.0` and `tag/v2.0.0`); `mach.lock` is gone. The debug profile
  builds without debug info, as the compiler's own does, because the windows
  target registers no debug model in 5.0 and a profile is not per target.
- source: every `Result` / `Option` use is a `res` / `opt` tag read through
  `sel` guards; std 2.0 signatures (allocation, paths, strings, env, clock,
  writer sinks, toml/json optionals) are followed at each call site. The
  server's own error payload stays `str`, since its errors are client-facing
  messages.
- analysis: the standalone buffer path runs `editor.analyze` per phase and
  borrows the buffer's products for the request; diagnostics read the owned
  `DiagnosticStore` (children, related, fixes are vectors).
- types: the record back-link keys on the declaring file and interned name the
  5.0 type table carries (`TypeOwner`), with `nominal_decl` recovering the
  declaration from the file's Ast; `FieldKey` and `RecordSite` carry the same
  identity.
- project: `site_of` canonicalizes a `use` / `fwd` binding, which no longer
  carries a DeclId, to the defining module's own symbol; every consumer of a
  site reads `site.sym`.
- test: protocol fixtures are 5.0 manifests; the on-disk edit in the watcher
  case keeps the project compiling.

### Requires
- mach v5.0.2: the project load uses `driver.analyze_project_tolerant`, which
  keeps a project's trees, resolve results and sema results through a rejected
  frontend phase (briar-systems/mach#3337). The document view accepts a
  module without a sema product, so a buffer mid-edit keeps project-scoped
  diagnostics and navigation. Types in independent modules later in
  dependency order still go dark while another module is rejected
  (briar-systems/mach#3340). Buffers are registered with the editor under
  their filesystem path, as 5.0 requires; a project whose manifest cannot
  load is analyzed standalone by the editor (briar-systems/mach#3343), which
  keeps syntax features alive while a manifest is being edited.

## [0.17.0] - 2026-08-31

Completion now stays responsive while project analysis catches up to edits,
without combining offsets, syntax, or types from different revisions. Protocol
replies and module re-exports are also classified at their actual boundaries.

### Fixed
- completion: a request queued behind an edit no longer waits for a synchronous
  project rebuild. When the loaded project snapshot is stale, completion uses
  one exact analysis of the current editor buffer and marks the result
  incomplete because dependencies are unavailable there. A current project
  snapshot still supplies dependency-aware names, while a cold project keeps
  the existing strict load path. This preserves revision identity throughout
  an answer and leaves the deferred diagnostics pass to rebuild the project.
  (#227)
- completion: module members now come from the resolver's complete public
  prefix. A module that declares its own public names and also forwards names
  from another module offers both sets, rather than dropping the forwarded
  exports. (#227)
- protocol: valid replies to server-initiated watcher and progress requests no
  longer fall through the request backstop and receive a spurious
  `InternalError`. A response is recognized only when it has an ID, has no
  method, and contains exactly one of `result` or `error`, so malformed
  methodless envelopes still receive `InvalidRequest`. (#226)

### Changed
- trace: the module documentation now names the body opt-in that the server
  actually reads, `MLS_TRACE=bodies`.

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
