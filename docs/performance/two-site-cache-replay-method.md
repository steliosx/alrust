# Replaying Alerta dashboard traffic at two sites

This diagnostic compares a direct request to the site's HAProxy with a request to a one-second Varnish cache immediately in front of the same HAProxy. A miss takes the ordinary application path. Site B crosses the emulated WAN to the writer's site. Each data site has its own cache, HAProxy, PgBouncer and two Alerta app processes. Patroni manages one writable PostgreSQL server and one asynchronous standby. Site C has an etcd voter. The experiment uses loopback processes on one host. Use separate VMs to measure your deployment's capacity. [Namespace packet shaping](../../scripts/bench/dual-site/run_netem.sh)

## Choose a representative dashboard

Count dashboard instances per site, the refresh interval, and how often instances issue the same authenticated request within one second. The cache key includes the host, full URL, authorization identity and accepted encoding. Distinct tenant credentials, relative date filters that generate a new URL, and different query strings can all reduce reuse. Count those locally. Keep raw credentials and customer URLs out of the results.

| Trace | What stays the same at each site | Question |
|---|---|---|
| `shared-5s` with aligned refresh | Eight panels divided into four matching pairs | Can simultaneous viewers share an origin request? |
| `shared-5s` with spread refresh | Same credentials and filters, matching viewers staggered by 2.5 s | Can spreading refreshes help without caching? |
| `personal-5s` | A separate credential per panel at a given site | What does the extra proxy cost when requests cannot share? |
| `mixed` or `wallboard` | Two or four matching list requests in a four-reader wave | How does the remote dashboard respond to different degrees of sharing and write traffic? |

The five-second fixed trace holds the offered schedule constant across arms. It contains 16 panel refreshes and four writes every five seconds across both sites. The completion-paced trace starts a panel's next timer when its list request finishes. A slower panel can therefore complete fewer refreshes in the same window. Always report the number of completed refreshes and writes in each arm. The pinned Web UI also starts an environments query before the list and supports user-selected page sizes and relative date filters. The optional two-request test times the later of the list and environments responses while keeping the next timer attached to the list. It uses a synthetic API-key client and a 100-row page. [Refresh timer](https://github.com/alerta/alerta-webui/blob/2687e4afa93b41c4fa1d46201bba9ea38be0d491/src/views/Alerts.vue#L389) · [Query construction and pagination](https://github.com/alerta/alerta-webui/blob/2687e4afa93b41c4fa1d46201bba9ea38be0d491/src/store/modules/alerts.store.ts#L108)

### Estimate reuse locally before running an experiment

Assign opaque numeric labels locally for site, credential identity and exact representation (host, full request target, encoding and any varying headers). Write one chronological JSON line per dashboard GET. Use `eligible=false` for a request that the intended policy must pass through. This example describes two matching requests arriving 200 ms apart at the same site.

```jsonl
{"time_ms":0,"site":0,"identity":5,"representation":3,"eligible":true}
{"time_ms":200,"site":0,"identity":5,"representation":3,"eligible":true}
```

Run `python3 scripts/bench/dual-site/cache_replay_fit.py trace.jsonl --ttl-ms 1000 --fill-ms 100` on the machine holding the trace. The result separates requests that would reach the origin, wait for an in-flight fill, or find a warm object. The model starts empty and assumes successful fills, a fixed time to make an object shareable, sufficient cache space and no invalidation. It produces a reuse estimate. Use the live trial for latency, correctness and freshness. [Offline estimator](../../scripts/bench/dual-site/cache_replay_fit.py)

## Run a matched pair

The manual hosted workflow builds the reference Alerta environment from the repository lock files and installs the pinned proxy and quorum binaries. Its serial matrix enforces a two-CPU quota and a 4 GiB memory limit on a single Ubuntu VM. Each pair starts fresh inside its own network and process namespaces. [Hosted setup](../../scripts/bench/dual-site/hosted-setup.sh) · [Experiment order](../../scripts/bench/dual-site/hosted_matrix.py) For the tuned arm, the tested add-on was 13,764 bytes with SHA-256 `9d92181b0109cfe9974dfee05109117d24abeaa7686ad3d0305a2342a2327062`. The same add-on mode applies to both cache arms. Use `--features stock` to try stock Python Alerta. The lab VCL accepts generated benchmark keys on explicitly listed GET routes. It passes cookies, other credentials and writes to the origin. [Exact add-on](https://github.com/steliosx/alrust/blob/9f49d09cfcda9eb9f503ac5f36b4e8100b964b56/scripts/bench/alerta_fast.py) · [Benchmark VCL](../../scripts/bench/dual-site/cache_replay.vcl)

Set the binary, virtual environment, add-on, private temporary directory and *new* evidence directory paths to match your isolated lab. Supply a CPU set available on that host. Run this example as an ordinary user on a Linux machine with an available coretemp sensor, a user systemd manager and subordinate UID/GID mappings. The measured laptop used one quarter of one CPU for all services and the load generator together, two allowed logical CPUs, and a utilization clamp of 160. Keep the physical resource budget identical across arms. A different budget can move the bottleneck.

```sh
export TMPDIR=/lab/private-tmp
export BENCH_VARNISH_PREFIX=/usr
export REPLAY_PROFILE=shared-5s REPLAY_CADENCE=aligned REPLAY_SEED=12
export REPLAY_PROBE_ROUTE=dashboard REPLAY_FEEDBACK=0 REPLAY_NEWEST=0
export SQL_RTT_MS=50 WAL_RTT_MS=100 DCS_RTT_MS=30
export JITTER_MS=4 LOSS_PERCENT=0.1 HTTP_RATE_MBIT=4
d=scripts/bench/dual-site
systemd-run --user --scope -p CPUQuota=25% -p CPUQuotaPeriodSec=5ms \
  -p MemoryMax=1536M -p MemorySwapMax=0 -p RuntimeMaxSec=26min \
  timeout --signal=INT --kill-after=25s 25m bash "$d/run_netem.sh" \
  taskset -c 2,3 uclampset -M 160 /lab/venv/bin/python "$d/run_cache_replay.py" \
  --etcd /lab/bin/etcd --haproxy /lab/bin/haproxy --pgbouncer /lab/bin/pgbouncer \
  --pg-bin /lab/postgres/bin --psql /lab/postgres/bin/psql --venv /lab/venv \
  --addon /lab/addon/alerta_fast.py --output /lab/results/pair-12 \
  --features reuse+onecount+nolog+nosentry --ingress primary-aware \
  --dashboard-encoding gzip --read-filter all --page-size 100 \
  --seed-alerts 32 --bulk-alerts 240 --key-count 13 --client-idle 3 \
  --health-interval 1000 --health-timeout 4000 --pool-query-wait 4 \
  --writer-idle 120 --pool-idle 60 --pool-login-retry 2 --client-inflight 24 \
  --rtt-ms 50 --wal-rtt-ms 100 --dcs-rtt-ms 30 --cool-start-c 69 \
  --rates 4 --seconds 50 --repeats 1
```

Use a new directory for each invocation. Change the seed from even to odd to reverse arm order. Under `REPLAY_NEWEST=1`, one out of four timed writes is a new resource and the other writes advance duplicate ordinals. The list sorts by last receive time. The offline oracle reconstructs possible tenant-specific pages from acknowledged writes and compares the final ordering and receipt ledger with PostgreSQL. `REPLAY_FEEDBACK=1` enables completion pacing on the five-second profiles. `REPLAY_ENVIRONMENTS=1` adds the concurrent environments query to the stable panel fixture. `REPLAY_WRITE_ROUTE=direct` sends writes to HAProxy in both arms while reads still test direct versus cached entry. [Trace generator](../../scripts/bench/dual-site/cache_replay_model.py) · [Driver](../../scripts/bench/dual-site/run_cache_replay.py)

## Decide whether caching is worthwhile

1. Compare remote *and* local dashboard p95 from scheduled refresh to completion. Record ingest acknowledgement p95 alongside it. For requests that fully bypass shared cache, quantify any added proxy time. For the two-request journey, measure completion of both requests.
2. Count eligible reads, hits, origin fetches, direct writes, errors and completed refreshes. Cached reads skip the application's API-key usage and query accounting. Check the impact on any custom request hooks you depend on.
3. Warm both sites, post a new alert, then check the first page containing its ID and the correct totals. With a different temporary read-only key, warm both sites, revoke that key, then check when each site first denies access. Report the last observed stale response and first correct response relative to acknowledgement. Poll spacing, origin latency and the WAN all contribute to those client-observed times.
4. Retain failed trials in the reliability record. A successful status alone does not verify a dashboard. Check the returned tenant, ordered IDs and counts and reconcile acknowledgements against both databases. Count partial or uncertain writes before retrying. A healthy asynchronous run cannot establish failover RPO.

The bundled verifier recomputes the measured series offline. For a new lab's single result directory, it can also verify the paired records directly:

```sh
PYTHONPATH=scripts/bench/dual-site python3 - /lab/results/pair-12/cache-replay.json <<'PY'
import json
import sys
from pathlib import Path
from summarize_cache_replay import pair_cells
print(json.dumps(pair_cells(json.loads(Path(sys.argv[1]).read_text())), indent=2))
PY
```
