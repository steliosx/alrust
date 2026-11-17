#!/usr/bin/env bash
# Disposable alpha Compose smoke, proposed for standard GitHub-hosted x64 only.
# These environment guards prevent accidents; they are NOT a security boundary,
# fork/controller isolation proof, or authority to execute with spoofed variables.
# No cleanup guarantee is possible after a hard job kill or runner retirement.
set +x
set -euo pipefail
umask 077

refuse() { printf '%s\n' 'compose-smoke: refused (runner/source precondition)' >&2; exit 1; }
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted &&
   ${RUNNER_OS:-} == Linux && ${RUNNER_ARCH:-} == X64 ]] || refuse
[[ ${GITHUB_RUN_ID:-} =~ ^[0-9]{1,20}$ && ${GITHUB_RUN_ATTEMPT:-} =~ ^[0-9]{1,20}$ &&
   ${GITHUB_SHA:-} =~ ^[0-9a-fA-F]{40}$ ]] || refuse
[[ -n ${GITHUB_WORKSPACE:-} && -d $GITHUB_WORKSPACE && -n ${RUNNER_TEMP:-} && -d $RUNNER_TEMP ]] || refuse
workspace=$(cd -- "$GITHUB_WORKSPACE" && pwd -P)
[[ $(pwd -P) == "$workspace" ]] || refuse
[[ $(git rev-parse --show-toplevel) == "$workspace" && $(git rev-parse HEAD) == "$GITHUB_SHA" ]] || refuse
[[ -f $workspace/docker-compose.yml && -f $workspace/scripts/alpha-smoke.py ]] || refuse
# The name inventories below are bound to this reviewed Compose shape. A changed
# shape requires affected source review, not silent reuse of an old inventory.
compose_digest=$(sha256sum -- "$workspace/docker-compose.yml")
[[ ${compose_digest%% *} == 3dc8accf534388af4b3a29ba9bcf612fdc21bc7e8fe36c9f049dfc8d397db363 ]] || refuse

# Commands below receive a fresh environment and an empty private Docker config:
# no caller Compose overrides, remote Docker context, credentials or app config.
private=$(mktemp -d -- "$RUNNER_TEMP/alrust-compose.XXXXXXXX")
mkdir -m 700 -- "$private/docker" "$private/home"
project="alrust-compose-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
compose_file="$workspace/docker-compose.yml"
base=http://127.0.0.1:8080
export ALRUST_ADMIN_KEY ALRUST_DB_PASSWORD
ALRUST_ADMIN_KEY=$(python3 -I -c 'import secrets; print(secrets.token_hex(32))')
ALRUST_DB_PASSWORD=$(python3 -I -c 'import secrets; print(secrets.token_hex(32))')
printf '::add-mask::%s\n' "$ALRUST_ADMIN_KEY" "$ALRUST_DB_PASSWORD"

# Raw stdout/stderr stay in mode-0700 runner-private custody. Never cat/upload
# these files: Compose output or tracebacks may contain generated credentials.
metadata() {
  python3 -I - "$1" "$2" "$3" "$4" <<'PY'
import hashlib, json, pathlib, sys
stage, rc, out, err = sys.argv[1:]
logs = {}
for stream, path in [('stdout', out), ('stderr', err)]:
    digest = hashlib.sha256()
    size = 0
    with pathlib.Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            digest.update(chunk)
            size += len(chunk)
    logs[stream] = {'sha256': digest.hexdigest(), 'bytes': size}
print(json.dumps({'stage': stage, 'exit': int(rc), 'logs': logs}, sort_keys=True))
PY
}
run_stage() {
  local stage=$1 seconds=$2 rc=0 metadata_rc=0
  shift 2
  if timeout --signal=TERM --kill-after=10s "${seconds}s" "$@" >"$private/$stage.out" 2>"$private/$stage.err"; then
    rc=0
  else
    rc=$?
  fi
  if metadata "$stage" "$rc" "$private/$stage.out" "$private/$stage.err"; then
    metadata_rc=0
  else
    metadata_rc=$?
  fi
  # Explicit propagation also works in if/OR contexts and cleanup's set +e.
  if (( rc != 0 )); then return "$rc"; fi
  return "$metadata_rc"
}
# timeout must run an external command: re-exec a fixed env + Docker argv,
# rather than exporting shell functions or accepting command/config inputs.
docker_stage() {
  local stage=$1 seconds=$2
  shift 2
  run_stage "$stage" "$seconds" env -i PATH=/usr/bin:/bin HOME="$private/home" \
    DOCKER_CONFIG="$private/docker" \
    ALRUST_ADMIN_KEY="$ALRUST_ADMIN_KEY" ALRUST_DB_PASSWORD="$ALRUST_DB_PASSWORD" \
    ALRUST_AUTH_REQUIRED=true ALRUST_HOUSEKEEPING_INTERVAL=0 \
    ALRUST_LISTEN=127.0.0.1 ALRUST_PORT=8080 ALRUST_BASE_URL="$base" \
    /usr/bin/docker "$@"
}
compose_stage() {
  local stage=$1 seconds=$2
  shift 2
  docker_stage "$stage" "$seconds" compose --project-name "$project" \
    --project-directory "$workspace" --env-file /dev/null --file "$compose_file" "$@"
}
objects_empty() {
  local prefix=$1 kind fail=0
  local -a listing
  for kind in container volume network; do
    listing=("$kind" ls --quiet)
    if [[ $kind == container ]]; then listing+=(--all); fi
    if docker_stage "$prefix-$kind" 15 "${listing[@]}" \
      --filter "label=com.docker.compose.project=$project"; then
      [[ ! -s $private/$prefix-$kind.out ]] || fail=1
    else
      fail=1
    fi
  done
  return "$fail"
}

names_empty() {
  local prefix=$1 include_image=$2 fail=0 kind filter
  local -a kinds=(container volume network)
  if [[ $include_image == true ]]; then kinds+=(image); fi
  # Reject names irrespective of labels. Container prefix refusal is deliberately
  # conservative; Compose's two current service names are project-db-1 and
  # project-alrust-1. No pre-existing object may be adopted or removed.
  for kind in "${kinds[@]}"; do
    case "$kind" in
      container) filter="name=^/${project}" ;;
      volume) filter="name=${project}_alrust-pgdata" ;;
      network) filter="name=${project}_default" ;;
      image) filter='reference=alrust:alpha' ;;
    esac
    local -a listing=("$kind" ls --quiet)
    if [[ $kind == container ]]; then listing+=(--all); fi
    if docker_stage "$prefix-name-$kind" 15 "${listing[@]}" --filter "$filter"; then
      [[ ! -s $private/$prefix-name-$kind.out ]] || fail=1
    else
      fail=1
    fi
  done
  return "$fail"
}

# Do not adopt, stop or delete any object that already has this exact label.
# In particular the EXIT cleanup is intentionally NOT registered until all
# three successful queries prove absence. Inspection errors are not absence.
objects_empty before && names_empty before true || { printf '%s\n' 'compose-smoke: pre-existing objects or inspection failure; no cleanup registered' >&2; exit 1; }
cleanup() {
  local primary=$? failed=0
  trap - EXIT INT TERM
  set +e
  compose_stage cleanup-down 90 down --volumes --remove-orphans || failed=1
  objects_empty after || failed=1
  names_empty after false || failed=1
  if (( failed )); then
    printf '%s\n' 'compose-smoke: cleanup UNPROVEN' >&2
  else
    printf '%s\n' 'compose-smoke: scoped containers/volumes/networks absent after cleanup (labels and names); no image/VM/full-release claim'
  fi
  if (( primary != 0 )); then exit "$primary"; fi
  if (( failed )); then exit 1; fi
  printf '%s\n' 'compose-smoke: PASS (alpha Compose smoke and scoped cleanup only; 19 profiles NOT_RUN; ARM64 deferred)'
  exit 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
compose_stage pull-db 180 pull db
compose_stage build-alrust 1200 build --pull alrust
compose_stage up 120 up --no-build --pull never --wait --wait-timeout 90
# The alrust service has no healthcheck, so --wait returns once its container
# runs, possibly before the server listens. Wait for /management/gtg first.
run_stage ready 100 env -i PATH=/usr/bin:/bin /usr/bin/curl --retry 30 \
  --retry-connrefused --retry-delay 2 --max-time 5 -fsS "$base/management/gtg"
run_stage smoke 180 env -i PATH=/usr/bin:/bin HOME="$private/home" \
  python3 -I scripts/alpha-smoke.py "$base" "$ALRUST_ADMIN_KEY" --disposable-target
# An exit-zero process alone is insufficient: require real PASS cases and the
# existing smoke script's final zero-failure/zero-skipped summary. Emit only
# counts and a verdict, never smoke output or payloads.
python3 -I - "$private/smoke.out" <<'PY_SUMMARY'
import json, pathlib, re, sys
lines = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
passes = sum(line.startswith('PASS ') for line in lines)
failures = sum(line.startswith('FAIL ') for line in lines)
skips = sum(line.startswith('NOT_RUN ') for line in lines)
nonempty = [line for line in lines if line.strip()]
summaries = [re.fullmatch(r'(\d+) failure\(s\), (\d+) not run', line) for line in nonempty]
matched = [m for m in summaries if m]
valid = (bool(nonempty) and passes > 0 and failures == 0 and skips == 0 and len(matched) == 1
         and summaries[-1] is not None and matched[0].groups() == ('0', '0'))
print(json.dumps({'stage': 'smoke-summary', 'pass_cases': passes,
                  'fail_cases': failures, 'not_run_cases': skips,
                  'zero_failure_zero_not_run_summary': valid}, sort_keys=True))
sys.exit(0 if valid else 1)
PY_SUMMARY
# Success is emitted only after the EXIT trap proves scoped cleanup.
