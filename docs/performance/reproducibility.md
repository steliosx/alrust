# Reproducing the 13 November 2026 comparison

This note tells you what you can check from this repository about the
[Alerta and Alrust comparison](2026-11-13-alerta-comparison.md), and what a
full repetition needs. The comparison is a set of laboratory read pilots
(`LAB_PILOTS_ONLY`). It is not a production capacity, reliability, search or
integration result. All 19 formal runtime profiles are `NOT_RUN`.

## What you can check now

The published files are derived evidence: the report, the
[derived results](2026-11-13-results.json) and the
[PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md). Run this from
the repository root. It needs Python 3 and nothing else; it starts no
server and opens no network connection.

```sh
python3 -I scripts/check-performance-evidence.py \
  --report docs/performance/2026-11-13-alerta-comparison.md \
  --results docs/performance/2026-11-13-results.json \
  --manifest docs/performance/evidence-manifest.json \
  --root .
```

It checks that:

- each median paired ratio and ratio range in the results JSON follows from
  the per-run values stored beside it;
- each table in the report shows the values that follow from the results
  JSON, and each table names the same source commit and row count;
- the three published files are the reviewed bytes listed in
  [the evidence manifest](evidence-manifest.json).

A pass shows that the report and the JSON agree, and that the files are the
reviewed files. It does not show that the timings are correct: the
per-request samples and logs are not public.

## What is not public

The harness (seeding, request driver and response comparison), the VM
images, the runtime logs and the individual request samples stay in the
Codex laboratory. The `private_evidence` fields in the results JSON give
the SHA256 and size of each private run record, so a reviewer who is given
a record can check that it is the one the published numbers came from.

A portable harness with host details removed would be a separate change,
owned by Codex and reviewed like any other. Until then, a third party must
write their own harness. Their results are a new experiment that follows
the same method; they do not reproduce these numbers.

## What a repetition needs

Take every value from the report's "Exact subjects and environment" and
"Method" sections; this note does not repeat the figures. In short:

- **Alrust:** the merged-main arm is `8a30c19f`, which is public. The
  candidate arms `2c593304` and `784984b2` were never pushed to this
  repository, so you cannot rebuild them from it. The report gives the
  SHA256 of both release binaries.
- **Alerta:** the reference server at `f4312727` on CPython 3.12, with
  Gunicorn 27.0.1 and the worker settings in the report. The report says
  "the existing reference dependency hash locks" and does not name the
  file. `.github/reference-requirements.lock` is the reference runtime lock
  in this repository; confirm it against the private input manifest
  before you claim the same environment.
- **PostgreSQL 18.6** from the PGDG package revision named in the report,
  with the durability settings and shared buffers named there, for both
  servers.
- **One VM** with the CPU, memory and swap limits in the report. The
  server, database and client share it. Use the container image digest in
  the report.
- **The method:** seed through the reference API, check the row count and
  unique receive times, clone the same database for each server, alternate
  server order, and use the warm-up and timed-request counts in the report.

## Rules a repetition must keep

- Any request failure, or any difference in ordered IDs, selected content
  or counts, cancels the speed score for that run. Do not sort responses
  after measurement to hide an order difference.
- Send `sort-by=lastReceiveTime` explicitly on list queries. Alerta's
  default order is unspecified; Alrust always returns newest first.
- Publish the per-run medians and the paired ratios, not only the
  summary. The ratio is the median of the paired Alerta/Alrust ratios,
  not the quotient of the two displayed medians.
- Compare arms only at the same dataset size.
- Record peak host temperature and background load. They were not
  controlled in these pilots.

A noise-control rule and a matched-size three-arm design are proposed in
#225; they are not adopted yet.

## Provenance

The three published files come from Codex commit `da100d6e`, reviewed and
merged by #223 as `35b0380c`. The evidence manifest binds each file to
its SHA256 and size. If a later change edits one of them, the check above
fails until the manifest is updated in a reviewed change; the original
bytes stay in the history of `35b0380c`.
