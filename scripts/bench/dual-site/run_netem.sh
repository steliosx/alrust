#!/usr/bin/env bash
# Run a lab command in a new user/network namespace. The command runs as the
# invoking host user after NetEm is configured with namespace-local privileges.
set -euo pipefail

if (( $# == 0 )); then
    printf '%s\n' 'Usage: SQL_RTT_MS=50 WAL_RTT_MS=50 DCS_RTT_MS=0 run_netem.sh command args...' >&2
    exit 2
fi

user_id=$(id -u)
group_id=$(id -g)
user_name=$(id -un)
if (( user_id == 0 || group_id == 0 )); then
    printf '%s\n' 'Run this lab as an ordinary user.' >&2
    exit 2
fi
sub_uid=$(awk -F: -v name="$user_name" '$1==name && $3>0 {print $2; exit}' /etc/subuid)
sub_gid=$(awk -F: -v name="$user_name" '$1==name && $3>0 {print $2; exit}' /etc/subgid)
if [[ ! "$sub_uid" =~ ^[0-9]+$ || ! "$sub_gid" =~ ^[0-9]+$ ]]; then
    printf '%s\n' 'This host has no subordinate user/group mapping for the current account.' >&2
    exit 2
fi
sql=${SQL_RTT_MS:-50}
wal=${WAL_RTT_MS:-$sql}
dcs=${DCS_RTT_MS:-0}
jitter=${JITTER_MS:-0}
loss=${LOSS_PERCENT:-0}
http_rate=${HTTP_RATE_MBIT:-0}
if [[ ! "$http_rate" =~ ^(0|[1-9][0-9]{0,2})$ ]] || (( http_rate > 100 )); then
    printf '%s\n' 'HTTP_RATE_MBIT must be an integer from 0 (unlimited) through 100.' >&2
    exit 2
fi
for value in "$sql" "$wal" "$dcs" "$jitter"; do
    if [[ ! "$value" =~ ^(0|[1-9][0-9]{0,2})$ ]] || (( value > 100 )); then
        printf '%s\n' 'RTT and jitter must be integers from 0 through 100 ms.' >&2
        exit 2
    fi
done
if [[ ! "$loss" =~ ^[0-9](\.[0-9]{1,3})?$ ]] || ! awk -v loss="$loss" 'BEGIN {exit (loss > 5)}'; then
    printf '%s\n' 'Loss must be between 0 and 5 percent per direction.' >&2
    exit 2
fi
parent_network=$(readlink /proc/self/ns/net)

exec unshare --user --net \
    --map-users="0:$sub_uid:1" --map-users="$user_id:$user_id:1" \
    --map-groups="0:$sub_gid:1" --map-groups="$group_id:$group_id:1" \
    --setuid=0 --setgid=0 bash --noprofile --norc -c '
    set -euo pipefail
    parent=$1 uid=$2 gid=$3 sql=$4 wal=$5 dcs=$6 jitter=$7 loss=$8 http_rate=$9
    shift 9
    [[ "$(readlink /proc/self/ns/net)" != "$parent" ]]
    [[ "$(ip -br link | awk "{print \$1}")" == lo ]]

    ip link set dev lo up
    tc qdisc replace dev lo root handle 1: prio bands 5 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
    shape() {
        local band=$1 ip_address=$2 rtt=$3 rate=${4:-0}
        local args=()
        if (( rtt == 0 && rate == 0 )); then
            return
        fi
        if (( rtt > 0 )); then
            args+=(delay "$((rtt * 500))us" "${jitter}ms" loss "${loss}%" seed "$band")
        fi
        if (( rate > 0 )); then
            args+=(rate "${rate}mbit")
        fi
        tc qdisc add dev lo parent "1:$band" handle "${band}0:" netem "${args[@]}"
        tc filter add dev lo parent 1: protocol ip priority 10 flower skip_hw \
            dst_ip "$ip_address" classid "1:$band"
        tc filter add dev lo parent 1: protocol ip priority 10 flower skip_hw \
            src_ip "$ip_address" classid "1:$band"
    }
    shape 2 127.0.0.2 "$sql"
    shape 3 127.0.0.3 "$wal"
    shape 4 127.0.0.4 "$dcs"
    shape 5 127.0.0.5 "$sql" "$http_rate"

    export ALRUST_NETEM_ACTIVE=1 ALRUST_NETEM_HTTP_RATE_MBIT="$http_rate"
    export ALRUST_NETEM_RTTS="$sql,$wal,$dcs" ALRUST_NETEM_JITTER_MS="$jitter" ALRUST_NETEM_LOSS="$loss"
    exec setpriv --reuid="$uid" --regid="$gid" --keep-groups --no-new-privs "$@"
    ' netem "$parent_network" "$user_id" "$group_id" "$sql" "$wal" "$dcs" "$jitter" "$loss" "$http_rate" "$@"
