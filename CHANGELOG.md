# Changelog

## v1.0.0 - 2026-11-01

First finished version. Merges `feat/bill-json` into `main`.

### Added
- `netusage bill --json`: the cycle's cost split as data, for the Bills app.
- Unit tests for application kinds, a missing or corrupt bills file, network labels, byte
  formatting and totals on the phone app.
- MIT license, a Tests section and a "What it does not do" section in the README.

### Changed
- Bundles and their prices are recorded in the Bills app; `netusage bill add` and `base` point
  there instead of writing prices themselves.
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
