# Changelog

## Unreleased

### Changed
- The bill split is generic: the laptop's line takes its name from `share_label` in the bills
  file, and `netusage bill add` and `base` record bundles again.
- Application kinds for the bill can be extended from `~/.config/netusage/app_kinds.json`;
  `app_kinds.example.json` shows the format.
- Extra process names can be folded together from an `[aliases]` section in
  `/etc/netusage.conf`.
- Design notes no longer carry usage figures from the original machine.
- Released under the MIT license with the author's full name; CI runs both Python test
  scripts and the phone app's tests and debug build.

## v1.0.0 - 2026-11-01

First finished version. Merges `feat/bill-json` into `main`.

### Added
- `netusage bill --json`: the cycle's cost split as data.
- Unit tests for application kinds, a missing or corrupt bills file, network labels, byte
  formatting and totals on the phone app.
- MIT license, a Tests section and a "What it does not do" section in the README.

### Changed
- Bundle prices could be kept by another tool in the bills file.
- The dashboard launcher and window are named Internet Usage.

## Before v1.0.0

- 2026-10-26: `netusage bill` with the monthly cost split; per-app figures bounded by the
  interface totals; phone builds numbered from the commit count; the phone's plan projection
  uses the time actually gone.
- 2026-10-15: a watcher that logs which process is behind heavy downloads; the ISP dialog can
  add an extra bundle without retyping the reading.
- 2026-09-29: usage per network (Wi-Fi by router, wired by gateway), the phone app and family
  sharing.
- 2026-09-28: desktop dashboard, editable limits, internet-only per-app accounting, and the
  first import of the collector and the `netusage` command.
