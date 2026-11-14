# Alerta and Alrust comparison: 13 November 2026

Status: completed laboratory read pilots, submitted for independent review.
Alrust main had lower latency in all five tested cases. This is not a
production capacity, reliability, full-text search or integration claim.

## Exact subjects and environment

- Merged main: `8a30c19fd6663c42d7fdebc9adb86de276bda7d8`, Rust1.98.1.
- Measured unmerged Rust1.100 candidate: `2c5933045f053f6d1be9884e5b3dac9d72c8cb0f`.
  Final local candidate `784984b2fcd7856d2b2667b414bee8291c02d513` has test-only
  changes after that head; its release binary is byte-identical. The original
  pilot remains bound to its actual source, not retroactively relabeled.
- Alerta9.1.0: `f4312727783dad64198b62ed778bbca98a5144a3`; CPython3.12,
  the existing reference dependency hash locks and separately pinned Gunicorn27.0.1.
  Gunicorn: one gthread worker, four threads. Alrust SQLx pool:16.
- PostgreSQL18.6 for both arms, PGDG Debian package18.6-1.pgdg12+2.
  fsync, synchronous_commit and full_page_writes enabled;128MB shared buffers.
- Standalone disposable SmolVM: one vCPU,4GiB guest memory; host quota25% of
  one CPU,5GiB memory limit,no swap. Server, DB and client share that VM.
  No production data or external integration calls. One VM at a time.
- Python Bookworm amd64 image:
  `sha256:3c3ba8ad4c85eb63b7568f806434f14e33370056985da7fdf2673621cb1d7e93`.

## Method

Seed synthetic alerts through the reference API; verify the row count and
unique receive timestamps; clone that same database for each server. Cases:
first50 newest alerts, event=BackupFailed, severity=critical, counts, and an
empty selection. Explicit `sort-by=lastReceiveTime` on list queries makes the
reference's order match Alrust's fixed newest-first order. Alrust still ignores
that parameter; this does not test general sorting support.

Four repetitions alternate server order. Each case/target/repetition has five
warmups and25 timed requests:1,000 timed requests per pilot. A request failure
or mismatch in ordered IDs, selected content, total/severity/status counts
refuses the speed score. Both completed pilots had zero such failures.
These are warm authenticated business-data reads; API-key usage still writes.

Displayed latencies are medians of the four per-run medians. The ratio is the
median of four paired Alerta/Alrust ratios, not the quotient of displayed
medians. Per-repetition medians and ratios are in
[the derived results](2026-11-13-results.json). Higher ratio means lower Alrust
latency. Raw logs and individual runtime samples stay in private custody;
the derived JSON binds them with SHA256 and byte counts for independent review.

## Results

### merged main

Source `8a30c19fd6663c42d7fdebc9adb86de276bda7d8`, 10,000 alerts.

| Case | Alerta median (ms) | Alrust median (ms) | Median paired ratio | Ratio range |
|---|---:|---:|---:|---:|
| list | 158.73 | 79.44 | 2.00x | 1.93–2.02x |
| event | 130.30 | 56.90 | 2.32x | 2.21–2.41x |
| severity | 138.47 | 59.91 | 2.30x | 2.28–2.34x |
| count | 89.61 | 63.40 | 1.41x | 1.17–1.48x |
| empty | 70.66 | 17.98 | 4.08x | 3.51–4.57x |

### unmerged candidate

Source `2c5933045f053f6d1be9884e5b3dac9d72c8cb0f`, 1,000 alerts.

| Case | Alerta median (ms) | Alrust median (ms) | Median paired ratio | Ratio range |
|---|---:|---:|---:|---:|
| list | 85.91 | 27.00 | 3.20x | 2.34–3.90x |
| event | 89.19 | 22.54 | 4.03x | 3.51–4.18x |
| severity | 86.47 | 23.34 | 3.80x | 3.40–3.95x |
| count | 61.27 | 19.38 | 3.04x | 2.77–3.56x |
| empty | 62.50 | 11.14 | 5.66x | 4.39–6.36x |

Do not compare the candidate's1,000-row ratio with main's10,000-row ratio to
claim a Rust-upgrade speedup. A matched-size three-arm experiment is still needed.

Main binary:9,024,432 bytes, SHA256
`d6321f810e5f1798d8a626294062ddf2479c64f34f7c1095c768e06d19d2b379`.
Candidate binary:9,001,472 bytes, SHA256
`1419de55a5f0b9c210574ee69d64f4975345e2cca07fbf5eb61954189e7b2e57`.

## Validation and retained failures

Candidate784984b passes fmt, workspace/all-target Clippy with warnings denied,
release build and163 distinct executed tests. The initial local test run
reported163 passes but39 were early returns without DATABASE_URL; those39
subsequently executed successfully in isolated PG18.6, along with two repeated
static cases. Two catalog generators were intentionally ignored. All19 formal
runtime profiles remain NOT_RUN. The DB test binaries emitted a missing WEAK
GLIBC_2.39 warning in Bookworm, but loaded and passed all expected tests; this
is not a universal portability guarantee. Full Docker build remains untested.

Earlier attempts are retained: an unavailable PG package revision stopped setup;
a default-order mismatch stopped the next attempt before any Alrust timing.
The reference defaults to unspecified order; Alrust fixes newest-first. Explicit
ordering defined the later comparable workload; responses were not sorted after
measurement to conceal the mismatch.

Main pilot1558.77s; candidate pilot517.83s; DB tests VM271.98s. Successful service
exit and empty/absent workload cgroups were checked. Host peak100C occurred
across setup and measurements, so the resource cap is not thermal qualification.
The owner's temporary thermal exception expired on13 November2026 at15:48:31UTC;
these records do not authorize another bypass or restart automation.

## What this does not establish

No100k run, open-loop load test, write-throughput comparison, fault-recovery
campaign, p99 claim, external-provider qualification or live Zabalack/Voalack
performance result was performed. At the measured main, adapters are not
registered: the hook interface and some Zabalack policy functions exist, with
synthetic integration tests. A mock hook cannot substitute for an end-to-end
Alerta-plus-plugin versus Alrust-adapter comparison.

Alrust currently ignores unsupported `q` search. Unfiltered responses must never
be scored as successful fast search. Improved search needs query correctness,
relevance judgments, permission/redaction checks and measured plans first.
Y54 reference-environment promotion, ARM64 and public-release decisions are
unaffected. No consumed integration or historical reference file was removed.

## Next measurements and implementation

Start with a counts-only store path: `/alerts/count` currently obtains an unused
alert row through find_alerts. Preserve counts, filters, authorization and errors;
measure query count and matched output before a speed claim. Then examine
newest-first/filter query plans and write cost before selecting reviewed indexes.
Keep adoption/catalog validation and applied migrations intact.

The essential inbound/outbound implementation set remains Zabbix7.0 LTS
(minimum7.0.25), a generic inbound mapper including Veeam ONE, and a generic
outbound webhook. Zabalack/Voalack acknowledgment hooks remain a separate goal.
See [PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md) for the next
version experiment. Source and CI review remains required before publication.
