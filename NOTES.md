# netusage - design notes

Engineering record for the tool documented in `README.md`. Kept for whoever
picks this up next.

Built in August 2026 on Ubuntu 24.04, kernel 7.0, bpftrace 0.20.2, Python 3.12.

## Why it is built this way

**Two numbers from two sources, deliberately.** The cycle total comes from
kernel interface counters; the per-app split comes from eBPF socket probes.
They are collected independently and are not reconciled. The interface counter
is the number that matters for a data plan and it cannot be wrong, so it must
not depend on the fragile part. When eBPF fails, the total still works.

**Physical interfaces only.** A typical developer laptop has one or two
physical NICs and a couple of dozen virtual ones. Counting `docker0`, `br-*`, `veth*` would inflate the total badly, and
`tailscale0` would double-count because its traffic already leaves via the
WiFi adapter. Physical NICs are identified by `/sys/class/net/<if>/device`
existing, which no virtual interface has. Verified on a laptop with one wired
and one wireless adapter: it selects exactly those two.

**Counters are checkpointed, not just read.** The last raw counter value is
written to the database every interval. On restart the collector compares:
a lower value means a reboot or interface reset, so the current value is the
traffic since that reset; a higher value gives a normal delta. Without the
checkpoint every reboot would either lose a day or record a huge negative.

**Days, not cycles, are the storage unit.** A cycle is a date range over daily
rows. Changing `cycle_day` re-slices existing history instead of invalidating
it, and past cycles stay queryable.

**Hours alongside days.** `iface_hourly` gets the same deltas as
`iface_daily`, keyed `YYYY-MM-DD HH` in local time, for the dashboard's
"today by hour" chart. That schema and key format are a contract with the
dashboard; do not rename either. There is no per-app hourly table on purpose.

**vnstat is installed but nothing depends on it.** It is an independent
cross-check on the interface totals (`vnstat -m`), not a data source.

## Bug log

Worth reading before changing the probes or the main loop. Every one of these
passed local testing and only appeared once the service ran as root.

**Receive probes read 32-bit values at 64-bit width.** `tcp_cleanup_rbuf`
and `udp_recvmsg` hand back an `int`. Read as 64 bits they pick up whatever
the caller left in the high word, producing byte counts that are exact
multiples of 2^32 - the tell was `376.00 GB` (`94 x 2^32`) appearing minutes
after start. Both are now cast to `int32`. The send paths take a `size_t` and
were always correct, which is why uploads looked right while downloads did
not. A 64 GiB-per-batch sanity clamp now discards impossible values, so a
future ABI change degrades to missing data rather than fiction.

**Map `for` loops need bpftrace 0.21.** `probe_for.bt` is the better backend
(no print/clear gap) but Ubuntu 24.04 ships 0.20.2 and rejects the syntax.
`for_each_map_elem` appearing in the binary is the BPF helper, not evidence of
the script feature - do not infer support from it again. The version is now
checked up front so an unusable probe is not attempted and logged as an error
on every start.

**The main loop spun at 21.7% of a core.** It waited on an event meaning
"still running", but `Event.wait()` returns immediately when the event is
already set, so the loop never slept and sampled thousands of times a second.
The recorded totals stayed correct the whole time - deltas simply got smaller
and summed the same - so nothing in the data revealed it. It is now a stop
flag, measured at 0.06%. `test_loop_paces` guards this.

**Averaging over elapsed days understates a partial cycle.** Dividing by days
elapsed rather than days with data reported a small fraction of the real
daily figure. Coverage percentage had the same flaw and now compares against
interface bytes only for days that have per-app rows.

**Per-app bytes included traffic that never reached a NIC.** The socket
probes count every socket byte, and a socket does not know whether its peer
is across the world or on 127.0.0.1. On some days per-app summed to more than
ten times the interface total; the crash-report daemon `whoopsie` alone
claimed more than a whole cycle's traffic on another, with `postgres`, `jest`
and `next-server` adding several GB each - all loopback, docker bridge or
tailscale tunnel traffic. The 2x repair in
`install.sh` hides the worst days but not the cause. `probe_filtered.bt` now
reads `sk->sk_dst_cache->dev->ifindex` and keeps the event only when that is
a counted physical NIC; with no cached route it drops IPv4 127/8 and
self-addressed peers (and, for UDP, a 127/8 address in `msg_name`). Tailscale
bytes then appear once, under `tailscaled`, as the encrypted WiFi traffic.
Container sockets are judged in their own namespace (their ifindex numbers
mean nothing on the host): loopback and RFC 1918 peers dropped, the rest
counted. It is tried first and falls back to `probe_print.bt` if it does not
attach within 15 s, so a struct the kernel renames degrades to the old
behaviour, visible in `netusage status`. History before `meta.app_filter_since`
still includes local traffic and cannot be separated after the fact.

**Process naming, found by running the classifier over every live PID.**
Kernel threads (`cpuhp/2`) collapsed to `"2"` because the `name/cpu`
convention was treated as a path; `postgres: checkpointer` leaked its argv
status line; the same program appeared under two names depending on launch
path. Naming is now an exact alias table, then distinctive
cmdline markers, then interpreter script resolution, then the plain
executable name. Extra names can be folded together from the `[aliases]`
section of `/etc/netusage.conf`. Later additions: a binary named after its
version (`~/.local/share/<tool>/versions/1.2.3`) takes the directory above,
skipping `versions`, so it maps to `<tool>`; and a process that exited
before its batch was named leaves only a thread comm, so `Chrome_*` and
`chrome_*` map to `chrome` (a guess: Electron apps use the same thread names).

## Verification

`/usr/bin/python3 test_netusage.py` - no root required. Covers cycle
boundaries including year rollover and leap February, counter behaviour across
reboots and daemon restarts, hourly rows and their key format, probe ordering
and placeholder substitution, process naming against every process on the
machine, notification thresholds and their persistence, loop pacing, and byte
formatting.

`/usr/bin/python3 test_data.py` covers the dashboard's query layer
(`netusage_data.py`): period and cycle boundaries, stepping between periods,
hourly/daily/monthly buckets, coverage, CSV export, a database from before
the hourly table, and the live meter across a counter reset.

Not covered: the eBPF probes themselves, which need root to attach. Their
output parsing is tested, the probes are not. This is exactly where the bugs
were, so treat probe changes as unverified until observed on a running
service.

## Operational notes

`install.sh` copies to `/opt/netusage`; the `netusage` command is a symlink to
that copy, not to this directory. **Editing files here changes nothing until
`sudo ./install.sh` is re-run.** The installer is idempotent and doubles as the
update path.

It also migrates: config keys added after a config was first written get
appended without touching existing values, per-app rows whose daily total
exceeds twice the interface total are dropped as corrupt (only up to 31 Aug
2026, the day the width bug was fixed: later days can exceed 2x from local
traffic, and dropping them would lose real history on every reinstall), and
app names older collectors recorded as a Chrome thread name are merged into
`chrome`.

## Dashboard

`gui/netusage_dashboard.py` with `gui/charts.py`, over `netusage_data.py`.
GTK 4 + libadwaita through the system interpreter, which is the one that has
the bindings; the `python3` on a developer PATH may not.

Charts are drawn with GSK paths into a `Gtk.Snapshot`, behind a small pen
object that mimics the cairo calls. Plain cairo drawing from Python needs
`python3-gi-cairo`, which Ubuntu does not install by default, and the
dashboard should not need a package install to open.

**Limits are a separate user-owned file.** The collector runs as root and
the config in /etc is root's, but limits are something to change on a whim
from the dashboard. `/var/lib/netusage/limits.conf` belongs to the desktop
user; the collector only reads it, checks its mtime every interval, parses
it as a whole (a half-written or broken file is ignored and the previous
limits stay), and publishes what it enforces into meta so the CLI and
dashboard never show a limit that is not actually active. The fired-threshold
record per day/cycle also stores the limit it was fired against, so changing
the limit re-arms the thresholds.

Live throughput reads `/sys/class/net/*/statistics` directly each second
rather than the database, which only moves every 10 seconds.

To check it visually without clicking through, render each page to PNG with
`Gtk.WidgetPaintable` + `renderer.render_texture`; that is how the layout was
reviewed.

## Open items

- Coverage on the day the width bug was repaired reads well below the
  expected 90-95%: the repair deleted that day's per-app rows while interface
  totals kept the full day, leaving a gap that cannot be backfilled. If
  coverage stays low on later full days, the probes are missing traffic.
- `probe_print.bt` prints then clears the kernel map. Traffic arriving between
  those two operations is lost - sub-millisecond against a 10 second interval,
  well under 0.1%, and it affects only the per-app split. `probe_filtered.bt`
  uses the same print format and has the same gap; it stays ahead of
  `probe_for.bt` even on bpftrace 0.21, since excluding local traffic matters
  far more. A `for`-loop copy of the filtered probe would close the gap once
  0.21 is installed.
- `probe_filtered.bt` has only been desk-checked against the kernel's BTF
  (struct fields exist, argument positions match); it has never attached.
  After install, `netusage status` should say "physical NICs only" and
  `journalctl -u netusage -n 30` should show no "trying next variant". Then
  compare per-app against interface totals for a full day: per-app should now
  sit below the interface figure.
- The ifindex list is fixed when bpftrace starts. A USB tether plugged in
  later is counted in the interface total (if `interfaces = auto` picked it up
  at start) but not in the per-app split until a restart.
