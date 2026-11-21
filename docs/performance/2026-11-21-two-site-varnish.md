# Does a one-second Varnish cache help an Alerta dashboard across two sites?

In the laptop trial, eight synchronized wallboards at the remote site shared four authenticated pages. Varnish reduced remote dashboard p95 from **2.10 s to 1.47 s**. All 40 remote cache hits arrived while a matching origin request was still in progress. Those hits took 931 ms at the median. In a separate, spaced mixed trace, all eight remote hits arrived after the origin request had completed and took 6 ms at the median. For an operator choosing a refresh policy, count how many viewers arrive during a fill and how many arrive while the page is already warm. The two traces also differed in their filters and write mix.

## Laptop measurements

Each row represents one cache-off and one cache-on run with the same arrivals and fixture. The latency clock runs from scheduled arrival to validated response. Sites A and B both served operator reads through HAProxy at the writer's site A. The app, PgBouncer, PostgreSQL, Patroni, three etcd members, two caches and load generator shared one laptop. The CPU quota was 25% of one CPU. The remote HTTP link was configured for 50 ms RTT, 4 ms jitter, 0.1% loss in each direction and 4 Mbit/s. There were 272 synthetic alerts across four tenants. Both arms used the same Python add-on.

| Workload | Remote reads per arm | Remote dashboard p95, off → on | Local dashboard p95, off → on | Remote ingest p95, off → on |
|---|---:|---:|---:|---:|
| Shared wallboards, five-second refresh, aligned | 80 | 2,104 → 1,470 ms | 1,204 → 1,145 ms | 770 → 208 ms |
| Mixed views, aligned | 32 | 1,346 → 1,135 ms | 1,070 → 925 ms | 1,320 → 1,066 ms |
| Mixed views, spread, newest-first with mutations | 32 | 424 → 299 ms | 305 → 235 ms | 335 → 374 ms |

The wallboard cache avoided 80 of 200 origin requests across both sites. In the newest-first trial, 128 timed pages across the two arms matched tenant-specific database snapshots consistent with observed writes. The verification also reconciles duplicates, fresh alerts, API-key and query accounting and the final standby state. The direction of the ingest result varied across traces. These measurements give no general ingest-speed estimate.

**Freshness and access revocation.** In a separate warm-page probe after the newest-first run, the remote site still returned the old page at 879 ms and first showed the new alert at 1,134 ms. After revocation, it still served the warmed key at 861 ms and first returned HTTP 401 at 1,395 ms. These are sampled client-observed completion times after the operation was acknowledged. A one-second object TTL does not guarantee one-second visibility or denial. Send authenticated traffic directly to Alerta when its hooks or revocation must take effect on every request.

Three of ten laptop trials qualified, contributing 720 timed arrivals. Seven exclusions remain in the evidence. One trial lost a connection during a probe, one had an unclassified non-JSON write response, and the others encountered thermal stops or were interrupted. The laptop reached 99 °C during one excluded trial. Local integration testing stopped. The shared-host design, one pair per workload, small write counts, synthetic authentication and healthy writer limit the conclusions. These trials did not measure physical WAN capacity or failover. [Derived results](2026-11-21-two-site-varnish-results.json) · [Compressed synthetic receipts](2026-11-21-two-site-varnish-samples.json.gz)

## Reproduce the comparison

Start by counting matching requests *per site and identity* within the TTL. Keep the same credentials, URLs, site routing and writes between arms. The runner can vary panel phase, issue separate credentials, include the environments request alongside the list, or drive refreshes from completion. Report the completed refresh count, both sites' p95s, origin requests, errors and the client-observed stale interval for new alerts and revoked keys. The included offline verifier checks the trial and reconstructs the published measurements. [Method and commands](two-site-cache-replay-method.md) · [Hosted matrix](../../scripts/bench/dual-site/hosted_matrix.py)
