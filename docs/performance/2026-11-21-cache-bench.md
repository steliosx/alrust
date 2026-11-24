# A response cache in front of Alerta: Varnish and HAProxy at one site, 21 November 2026

One full run of the `cache` suite of the `bench compare` workflow. The suite puts a short-lived
response cache in front of two Alerta servers and measures what that gives. The numbers are
diagnostic and relative to one hosted runner. They are not a capacity claim. The cache rules are a laboratory set for this benchmark's
two synthetic keys, not a production configuration.

## Answer in short

- **A cache hit does not reach Alerta, so a hit can be stale.** For up to the expiry, the cache gives
  the stored answer. It does not run Alerta's check of the key (expiry and deletion) or of the
  customer scope, and it does not update the key's use count or last-used time. The operator
  chooses the expiry, and that choice sets how stale an answer can be.
- **Measured staleness, by expiry.** A deleted key still got a 200 answer until the times below:

  | Expiry | Last 200 seen, s (cache 0 / cache 1 / front) | First 401 seen, s (cache 0 / cache 1 / front) | Limit for the first 401, s |
  |---:|---|---|---:|
  | 1 s | 0.986 / 0.986 / 0.987 | 1.045 / 1.053 / 1.062 | 1.75 |
  | 2 s | 1.961 / 1.962 / 1.962 | 2.021 / 2.029 / 2.037 | 2.75 |
  | 5 s | 4.987 / 4.988 / 4.988 | 5.047 / 5.056 / 5.065 | 5.75 |

  The first 401 came 0.06 to 0.08 s after the last 200 in each expiry. The real end of the stale answers
  lies between those two observations. After a write, the new count appeared on both caches and the front
  within 0.71 s (1 s expiry), 1.69 s (2 s) and 4.72 s (5 s). Before the new count, 29, 86 and 263 old
  answers were seen, and none after it.
- **Repeated URL: much faster.** With one URL and one key, the cache that the key hashed to answered
  almost every request from its store. The median requests per second were about 74 to 80 times
  those of `alerta` for `count` and about 114 to 117 times for `list` (per-repetition values below).
  The expiry did not change this speed. The backend requests per timed run were 15, 8 and 3 for expiries
  of 1, 2 and 5 s. These hit rates measure Varnish, HAProxy and the load tool on one machine, not Alerta.
- **Never-repeated URL: no gain from the cache.** Every request was a miss (hit ratio 0.0000).
  Requests per second were +2.7 %, +10.2 % and +1.8 % against `alerta` in the three repetitions, but
  no hit caused that: `alerta`'s own repetitions ran from 383.3 to 408.9, and the run does not
  explain the difference.
- **Duplicate writes through the cache front: slower.** The writes pass the cache. Requests per second
  changed by -2.7 %, -1.2 % and -4.9 % against `alerta`, in the three repetitions.

## How this report was made

- **Run:** GitHub Actions run 44875194302, attempt 1, workflow `bench compare`, mode `full`, suite
  `cache`, on `main` at `0579d64c313eef50f8cb002319516047d23c6419`. Started 2026-11-21T06:21:57Z (UTC).
  Started by hand (workflow_dispatch) on `main` by the project's agent, after the owner approved one full
  run of this suite. The review of this report is the first independent check of the result.
- **Artifact:** `bench-compare-44875194302-1`, 3420710 bytes, digest
  `sha256:d2593907d8e83c6f44c3451d22dbfa5b1f795fa0f5392f2f4d1bd602e39f8374` as GitHub reports it.
  GitHub keeps the artifact until 2026-11-28T06:57:48Z. The results file here stays.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, 15701 MiB of memory, image
  `ubuntu24 20261116.133.1`. Servers, caches, HAProxy, PostgreSQL and the load tool share it, over
  loopback.
- **Software:** PostgreSQL 18.7 (PGDG package `18.7-1.pgdg24.04+2`), Alerta 9.1.0 from the pinned
  `reference/alerta` submodule (`f4312727`) with no change to its code and no add-on, gunicorn 27.0.1,
  PgBouncer 1.26.1, Varnish 7.1.1 (revision 7cee1c581bead20e88d101ab3d72afb29f14d87a), HAProxy
  2.8.30-0ubuntu0.24.04.2 (Ubuntu package), load tool oha 1.17.0.
- **Shape:** 5 arms, 3 repetitions, 78 cells. Warm-up 5 s, timed 15 s. Cases: `count` and `list` at 8 and
  32 clients; `list-unique` and `write-dup` at 8 clients only (four cases in all). The tables show the
  median of the three repetitions. 1000 seeded alerts, two synthetic keys.
- **Results file:** [2026-11-21-cache-bench-results.json](2026-11-21-cache-bench-results.json), the run's
  `results.json`, unchanged.

Arms, every one behind one HAProxy:

| Arm | What it is |
|---|---|
| `alrust` | One alrust process. A reference only. |
| `alerta` | The two Alerta servers, no cache, HAProxy round robin. |
| `alerta-cache-1s`, `alerta-cache-2s`, `alerta-cache-5s` | The two Alerta servers, each with one Varnish in front. HAProxy picks the cache by a hash of the `Authorization` header. The expiry is 1, 2 or 5 s. |

Each Alerta server is gunicorn with 2 worker processes of 4 threads (`2x4-pgbu-nofwd`). Its plugin list
has no `forwarder` (a configuration change). Its pgbouncer hop uses a Unix socket with `trust`, which
removes the per-request SCRAM exchange with the pooler. Each cache sends its misses to both Alerta
servers in turn.

The cache rules (the VCL in the suite) keep only this:

- GET only, no cookie, and only the two measured routes: `/alerts/count`, and the list URL alone or with
  the ignored `_` parameter.
- Exactly one credential header, with one of the two synthetic keys as its whole value, in the form
  `Authorization: Key <key>` or `X-API-Key: <key>`. Any other credential, a request with both headers, or
  the `api-key` query parameter goes to Alerta.
- Only a 200 answer without `Set-Cookie` is kept. Nothing is served after the expiry (no grace).
  Alerta 9.1.0 sends none of the response headers that could shorten the expiry, so those rules are
  written but not exercised by this run.

## Checks

- 78 of 78 cells valid; none missing, none twice; 0 required checks absent or failed (results.json `gate`).
- Gate self-test: 45 of 45 cases gave the expected result. The gate refused every case that removed or
  failed a required item, and passed the complete records.
- Each expiry passed its probe: the oracle (Alerta's answers equal the database's), isolation (on each
  cache alone, 34 requests per cache, 68 in all, with 16 expected hits), separation (through HAProxy),
  staleness, burst (32 requests at once gave 1 backend request) and revocation.
- Before each arm's cells, the suite's data check (`sane()`): the list, the list with `_`, and the count,
  each twice, must equal the database's answer and Alerta's direct answer. On each cache arm the second
  round must come from the cache. `known_hits` was 3 of 3 on every cache arm in every repetition
  (results.json `checks`, 15 of 15 entries passed).

## Results: requests per second and latency

Medians of three repetitions. Rows `count` and `list` repeat one URL with one key; the hit ratio is
1 minus backend requests divided by requests. The exact hit ratios are in the `cells` of results.json.

| case | c | arm | valid reps | rps | p50 ms | p99 ms | hit ratio | backend requests | server CPU ms/req | cache CPU ms/req | HAProxy CPU ms/req | postgres CPU ms/req |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| count | 8 | alrust | 3 of 3 | 6809 | 1.14 | 2.08 |  |  | 0.122 | 0.000 | 0.030 | 0.338 |
| count | 8 | alerta | 3 of 3 | 1005 | 7.88 | 11.63 |  |  | 1.761 | 0.000 | 0.082 | 1.209 |
| count | 8 | alerta-cache-1s | 3 of 3 | 74429 | 0.10 | 0.21 | 1.0000 | 15 | 0.000 | 0.018 | 0.019 | 0.000 |
| count | 8 | alerta-cache-2s | 3 of 3 | 75640 | 0.10 | 0.21 | 1.0000 | 8 | 0.000 | 0.018 | 0.019 | 0.000 |
| count | 8 | alerta-cache-5s | 3 of 3 | 75029 | 0.10 | 0.21 | 1.0000 | 3 | 0.000 | 0.018 | 0.019 | 0.000 |
| count | 32 | alrust | 3 of 3 | 7925 | 4.00 | 5.98 |  |  | 0.106 | 0.000 | 0.025 | 0.331 |
| count | 32 | alerta | 3 of 3 | 1043 | 28.36 | 59.20 |  |  | 1.740 | 0.000 | 0.081 | 1.364 |
| count | 32 | alerta-cache-1s | 3 of 3 | 82684 | 0.36 | 0.89 | 1.0000 | 15 | 0.000 | 0.018 | 0.017 | 0.000 |
| count | 32 | alerta-cache-2s | 3 of 3 | 83876 | 0.36 | 0.85 | 1.0000 | 8 | 0.000 | 0.018 | 0.017 | 0.000 |
| count | 32 | alerta-cache-5s | 3 of 3 | 83481 | 0.36 | 0.88 | 1.0000 | 3 | 0.000 | 0.018 | 0.017 | 0.000 |
| list | 8 | alrust | 3 of 3 | 1309 | 5.98 | 10.21 |  |  | 1.148 | 0.000 | 0.063 | 1.477 |
| list | 8 | alerta | 3 of 3 | 421 | 18.73 | 31.30 |  |  | 4.632 | 0.000 | 0.095 | 2.620 |
| list | 8 | alerta-cache-1s | 3 of 3 | 48521 | 0.15 | 0.33 | 1.0000 | 15 | 0.000 | 0.024 | 0.032 | 0.000 |
| list | 8 | alerta-cache-2s | 3 of 3 | 48614 | 0.15 | 0.33 | 1.0000 | 8 | 0.000 | 0.024 | 0.031 | 0.000 |
| list | 8 | alerta-cache-5s | 3 of 3 | 49198 | 0.15 | 0.33 | 1.0000 | 3 | 0.000 | 0.023 | 0.032 | 0.000 |
| list | 32 | alrust | 3 of 3 | 1392 | 22.74 | 31.90 |  |  | 1.154 | 0.000 | 0.059 | 1.483 |
| list | 32 | alerta | 3 of 3 | 444 | 63.76 | 157.85 |  |  | 4.699 | 0.000 | 0.103 | 2.734 |
| list | 32 | alerta-cache-1s | 3 of 3 | 50859 | 0.60 | 1.28 | 1.0000 | 15 | 0.000 | 0.025 | 0.032 | 0.000 |
| list | 32 | alerta-cache-2s | 3 of 3 | 50726 | 0.61 | 1.27 | 1.0000 | 8 | 0.000 | 0.026 | 0.032 | 0.000 |
| list | 32 | alerta-cache-5s | 3 of 3 | 51096 | 0.61 | 1.27 | 1.0000 | 3 | 0.000 | 0.025 | 0.032 | 0.000 |
| list-unique | 8 | alrust | 3 of 3 | 1314 | 5.98 | 10.12 |  |  | 1.148 | 0.000 | 0.063 | 1.470 |
| list-unique | 8 | alerta | 3 of 3 | 404 | 19.10 | 34.88 |  |  | 4.537 | 0.000 | 0.096 | 2.596 |
| list-unique | 8 | alerta-cache-2s | 3 of 3 | 420 | 18.82 | 29.41 | 0.0000 | 6303 | 4.756 | 0.254 | 0.095 | 2.640 |
| write-dup | 8 | alrust | 3 of 3 | 2877 | 2.77 | 3.06 |  |  | 0.315 | 0.000 | 0.052 | 0.312 |
| write-dup | 8 | alerta | 3 of 3 | 716 | 10.99 | 15.53 |  |  | 2.697 | 0.000 | 0.087 | 1.493 |
| write-dup | 8 | alerta-cache-2s | 3 of 3 | 697 | 11.39 | 15.62 | 0.0000 | 10454 | 2.719 | 0.178 | 0.079 | 1.494 |

The `server CPU` column is the Alerta (or alrust) process tree's CPU. On the `count` and `list` rows of a
cache arm it is 0.000, because no request reached Alerta for a hit. The cache arms' CPU figures include
that avoided work. The `list-unique` and `write-dup` rows reached Alerta and show non-zero server CPU.
Those two cases ran for the 2 s expiry only.

### Requests per second in each repetition

Change is against `alerta` in the same repetition.

| case | c | arm | rps r0 | rps r1 | rps r2 | median rps | change vs base, each rep |
|---|---:|---|---:|---:|---:|---:|---|
| count | 8 | alrust | 6749.7 | 6838.3 | 6809.1 | 6809.1 | +570.7%, +584.8%, +577.4% |
| count | 8 | alerta | 1006.4 | 998.5 | 1005.2 | 1005.2 | |
| count | 8 | alerta-cache-1s | 74429.3 | 75979.9 | 74191.4 | 74429.3 | +7295.6%, +7509.2%, +7280.8% |
| count | 8 | alerta-cache-2s | 75639.8 | 76061.3 | 74604.6 | 75639.8 | +7415.9%, +7517.3%, +7321.9% |
| count | 8 | alerta-cache-5s | 75029.3 | 76533.7 | 74765.5 | 75029.3 | +7355.3%, +7564.6%, +7337.9% |
| count | 32 | alrust | 7887.7 | 7924.9 | 7937.4 | 7924.9 | +653.5%, +661.6%, +660.9% |
| count | 32 | alerta | 1046.9 | 1040.5 | 1043.1 | 1043.1 | |
| count | 32 | alerta-cache-1s | 82683.5 | 82482.6 | 83501.2 | 82683.5 | +7798.2%, +7827.0%, +7904.8% |
| count | 32 | alerta-cache-2s | 83875.7 | 84461.5 | 83238.0 | 83875.7 | +7912.1%, +8017.2%, +7879.6% |
| count | 32 | alerta-cache-5s | 83933.1 | 83481.3 | 82800.5 | 83481.3 | +7917.6%, +7923.0%, +7837.7% |
| list | 8 | alrust | 1328.2 | 1309.0 | 1307.0 | 1309.0 | +225.6%, +207.4%, +210.6% |
| list | 8 | alerta | 407.9 | 425.8 | 420.7 | 420.7 | |
| list | 8 | alerta-cache-1s | 48557.4 | 48521.1 | 48449.0 | 48521.1 | +11802.8%, +11296.1%, +11415.3% |
| list | 8 | alerta-cache-2s | 48654.8 | 48614.0 | 48504.1 | 48614.0 | +11826.7%, +11317.9%, +11428.4% |
| list | 8 | alerta-cache-5s | 49197.9 | 49335.4 | 48725.3 | 49197.9 | +11959.9%, +11487.3%, +11481.0% |
| list | 32 | alrust | 1406.0 | 1392.1 | 1392.0 | 1392.1 | +216.7%, +222.1%, +212.1% |
| list | 32 | alerta | 443.9 | 432.2 | 446.0 | 443.9 | |
| list | 32 | alerta-cache-1s | 50808.1 | 50859.4 | 50904.6 | 50859.4 | +11344.7%, +11666.2%, +11313.5% |
| list | 32 | alerta-cache-2s | 50725.5 | 51355.8 | 50285.0 | 50725.5 | +11326.1%, +11781.1%, +11174.6% |
| list | 32 | alerta-cache-5s | 51095.7 | 51502.2 | 50635.6 | 51095.7 | +11409.5%, +11814.9%, +11253.2% |
| list-unique | 8 | alrust | 1318.6 | 1314.4 | 1309.0 | 1314.4 | +222.5%, +242.9%, +224.4% |
| list-unique | 8 | alerta | 408.9 | 383.3 | 403.6 | 403.6 | |
| list-unique | 8 | alerta-cache-2s | 419.8 | 422.3 | 410.8 | 419.8 | +2.7%, +10.2%, +1.8% |
| write-dup | 8 | alrust | 2876.6 | 2863.6 | 2876.6 | 2876.6 | +301.7%, +301.7%, +297.7% |
| write-dup | 8 | alerta | 716.1 | 712.9 | 723.2 | 716.1 | |
| write-dup | 8 | alerta-cache-2s | 696.6 | 704.5 | 688.1 | 696.6 | -2.7%, -1.2%, -4.9% |

## What the run shows

- **Repeated URL.** The caches answered almost every request. The median requests per second are about
  74 to 80 times those of `alerta` for `count`, and about 114 to 117 times for `list`. This is the best
  case for a cache. The hit rates measure Varnish, HAProxy and the load tool on one machine, not Alerta.
  Real traffic lies between this case and the next.
- **Never-repeated URL.** No gain from the cache. The caches stored nothing that was asked again (hit
  ratio 0.0000). The arm was +1.8 % to +10.2 % against `alerta` in each repetition; with no hit, the
  run does not explain that difference.
- **Duplicate writes.** The writes pass the cache, so the cache adds a hop and no benefit. The change is
  between -1.2 % and -4.9 % in each repetition (-2.7 % in the first).
- **Expiry.** The expiry (1, 2 or 5 s) did not change the speed of a repeated URL. It did change the
  backend requests per timed run: 15, 8 and 3. A longer expiry sends fewer requests to Alerta, and it
  allows a longer stale window (see the staleness table in Answer in short).

### How the counters were read

This run read each cache's counters at the moment the load tool stopped, before the one-and-a-half
second pause that follows the timed run. The gate checks the backend-request counter: as an exact count
for writes and new URLs, and as a bound (one tenth of the requests or fewer) for a repeated URL. The hit, miss and pass counters are not checked by the
gate (the suite's comment gives the reason). A later change (PR #277) reads the counters only when they
are steady. A quick run of another branch, before that change, showed one counter one short. This run
predates PR #277. All 78 cells of this run passed the gate as it was then. This report claims no more
than that.

## Limits

- Staleness and key checks: a cache hit skips Alerta's key and scope checks and its use-count update until
  the expiry. This run measured a deleted key only (not an expired key, not a change of customer scope),
  and one write, at three expiries, in one run. See Answer in short for the times. Choosing an expiry is
  an operator decision about staleness.
- One runner, one site, loopback only. The load tool, the caches and both Alerta servers share the four
  CPUs of the runner. Numbers are relative, not capacity.
- 1000 synthetic alerts, two synthetic keys, and one customer scope with 7 alerts.
- A laboratory cache rule set, not a production configuration. A site must decide its own rules, its own
  expiry and its own answer to the staleness and key-check gap. This suite has no invalidation or
  revalidation.
- The HAProxy version is the Ubuntu package given in the results (2.8.30-0ubuntu0.24.04.2). It is not the
  version used in the two-site lab.
- Three repetitions.
- The stale-answer times are the last 200 and the first 401 seen by a polling loop (a 0.05 s sleep between
  rounds, per the suite's code). The real end of the stale answers lies between them.

## See also

- [Alerta at two sites: where requests go, and `noecho`](2026-11-22-two-site-placement.md), the 22
  November run of the two-site lab.
- [Fastpath suite on a hosted runner](2026-11-21-fastpath-bench.md), the 21 November run of the
  duplicate-write path.
- [Saved searches under gunicorn](2026-11-22-savedq-load.md), the 22 November run of the opt-in `savedq`
  patch.
