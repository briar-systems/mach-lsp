# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
