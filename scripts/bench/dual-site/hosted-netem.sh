#!/usr/bin/env bash
# Called inside a fresh network and PID namespace on a disposable hosted runner.
set -euo pipefail
parent_net=$1 uid=$2 gid=$3
shift 3
[[ $EUID == 0 && $uid != 0 && $gid != 0 && $(readlink /proc/self/ns/net) != "$parent_net" ]]
[[ $(ip -br link | awk '{print $1}') == lo ]]
ip link set dev lo up
tc qdisc add dev lo root handle 1: prio bands 5 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
seed_support=0
if tc qdisc add dev lo parent 1:2 handle 20: netem delay 1ms seed 2 2>/dev/null; then
    tc qdisc del dev lo parent 1:2 handle 20:
    seed_support=1
fi
shape() {
    local band=$1 address=$2 delay=$3
    shift 3
    local args=(delay "$delay" 4ms loss 0.1%)
    if ((seed_support)); then args+=(seed "$band"); fi
    tc qdisc add dev lo parent "1:$band" handle "${band}0:" netem "${args[@]}" "$@"
    for field in dst_ip src_ip; do
        tc filter add dev lo parent 1: protocol ip priority 10 flower skip_hw "$field" "$address" classid "1:$band"
    done
}
shape 2 127.0.0.2 25ms
shape 3 127.0.0.3 50ms
shape 4 127.0.0.4 15ms
shape 5 127.0.0.5 25ms rate 4mbit
export ALRUST_NETEM_ACTIVE=1 ALRUST_NETEM_RTTS=50,100,30 ALRUST_NETEM_JITTER_MS=4
export ALRUST_NETEM_LOSS=0.1 ALRUST_NETEM_HTTP_RATE_MBIT=4 ALRUST_NETEM_FIXED_SEED="$seed_support"
exec setpriv --reuid="$uid" --regid="$gid" --init-groups --no-new-privs "$@"
