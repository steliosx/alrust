#!/usr/bin/env bash
# Run on a GitHub-hosted Ubuntu 24.04 VM after scripts/bench/setup.sh.
set -euo pipefail
[[ $RUNNER_ENVIRONMENT == github-hosted && $GITHUB_ACTIONS == true ]]
d=scripts/bench/dual-site
"$BENCH_PG_BIN/pg_ctl" -D "$BENCH_WORK/data" -m fast -w stop
v=7.1.1-1.1ubuntu1
sudo apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
  "varnish=$v" "libvarnishapi-dev=$v" build-essential libpcre2-dev zlib1g-dev iproute2
sudo systemctl stop varnish.service varnishncsa.service || true
"$BENCH_ALERTA_VENV/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps \
  --no-build-isolation -r "$d/patroni-extra.lock"
"$BENCH_ALERTA_VENV/bin/python" -m pip check
bin="$BENCH_WORK/bin"
mkdir -p "$bin"
fetch() { curl -fLsS --retry 4 --retry-all-errors -o "$2" "$1"; }
fetch https://www.haproxy.org/download/3.4/src/haproxy-3.4.7.tar.gz "$BENCH_WORK/haproxy.tgz"
fetch https://github.com/etcd-io/etcd/releases/download/v3.6.15/etcd-v3.6.15-linux-amd64.tar.gz "$BENCH_WORK/etcd.tgz"
(
 cd "$BENCH_WORK"
 sha256sum --check <<'SUMS'
ccfd342f3724c2cd659574044d788f44e20b212467c6bd5edbbfc517a2775dca  haproxy.tgz
b51d86e6bff2168de5ba8cd631c0fe6c785c7a1b32b083079f5677d6b7e2772c  etcd.tgz
SUMS
 tar xf etcd.tgz etcd-v3.6.15-linux-amd64/etcd
 install -m755 etcd-v3.6.15-linux-amd64/etcd "$bin/etcd"
 tar xf haproxy.tgz
 make -C haproxy-3.4.7 -j2 TARGET=linux-glibc USE_ZLIB=1 USE_PCRE2=1 haproxy > haproxy-build.log
 install -m755 haproxy-3.4.7/haproxy "$bin/haproxy"
)
"$bin/haproxy" -v
"$bin/etcd" --version
/usr/sbin/varnishd -V
"$BENCH_ALERTA_VENV/bin/patroni" --version
# The add-on is main's scripts/bench/alerta_fast.py (placement.py checks the same blob).
[[ $(git hash-object scripts/bench/alerta_fast.py) == 99adfccf560c6b162ad7e86b1796817d19602ba5 ]]
