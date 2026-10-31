#!/usr/bin/env bash
# Synthetic controls for forgehold-bind.sh: matching (argument, GITHUB_SHA),
# missing, short and mismatched expected commits, a dirty checkout, and git
# inspection failures (status, tree) that must refuse rather than pass with an
# empty value (FH-BIND-OBSERVE-1). Every failed assertion makes this script
# exit non-zero (FH-BIND-CONTROL-1). Uses a throwaway repository and, for the
# git-failure cases, a git shim on PATH that fails one subcommand and forwards
# everything else to the real git.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
bind=$here/forgehold-bind.sh
real_git=$(command -v git)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
"$real_git" -c init.defaultBranch=main init -q "$work/repo"
cd "$work/repo"
"$real_git" -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m one
head=$("$real_git" rev-parse HEAD)
other=$(printf '%040d' 1)
fail=0
# check NAME WANT-EXIT NEEDLE [--no-sha] [--git-shim MODE] -- ARGS...
check() {
  local name=$1 want=$2 needle=$3; shift 3
  local -a pre=(env)
  while [[ $# -gt 0 && $1 != -- ]]; do
    case $1 in
      --no-sha) pre+=(-u GITHUB_SHA) ;;
      --git-shim) pre+=(PATH="$work/shim-$2:$PATH"); shift ;;
      *) echo "bad option $1" >&2; exit 2 ;;
    esac
    shift
  done
  shift
  local out got
  set +e; out=$("${pre[@]}" "$bind" "$@" 2>&1); got=$?; set -e
  if [[ $got -ne $want ]] || ! grep -q -- "$needle" <<<"$out"; then
    echo "FAIL: $name (exit $got, wanted $want; output: $out)" >&2; fail=1
  elif [[ $want -ne 0 ]] && grep -q '"result":"PASS"' <<<"$out"; then
    echo "FAIL: $name printed a PASS line on a refusal" >&2; fail=1
  else
    echo "ok   $name"
  fi
}
make_shim() { # MODE: status | tree ; fails that subcommand with 128, forwards the rest
  local mode=$1 dir=$work/shim-$1
  mkdir -p "$dir"
  cat > "$dir/git" <<SHIM
#!/usr/bin/env bash
case "\$1 \$2" in
  "status --porcelain") [[ "$mode" == status ]] && exit 128 ;;
  "rev-parse HEAD^{tree}") [[ "$mode" == tree ]] && exit 128 ;;
esac
exec "$real_git" "\$@"
SHIM
  chmod 755 "$dir/git"
}
make_shim status; make_shim tree

check "matching argument"   0 "\"result\":\"PASS\",\"commit\":\"$head\",\"tree\":\"$("$real_git" rev-parse 'HEAD^{tree}')\"" -- j "$head"
GITHUB_SHA=$head check "matching GITHUB_SHA" 0 '"result":"PASS"' -- j
check "missing expected"    1 'missing or not a full 40-hex sha' --no-sha -- j
check "short sha"           1 'missing or not a full 40-hex sha' -- j "${head:0:12}"
check "mismatched"          1 "HEAD $head is not the expected commit $other" -- j "$other"
check "git status failure"  1 'git status failed' --git-shim status -- j "$head"
check "git tree failure"    1 'git rev-parse HEAD^{tree} failed' --git-shim tree -- j "$head"
touch untracked
check "dirty checkout"      1 'the checkout is not clean' -- j "$head"
rm untracked
exit $fail
