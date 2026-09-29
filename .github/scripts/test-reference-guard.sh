#!/usr/bin/env bash
# Regression cases for reference-guard.sh, run against disposable synthetic
# repositories. Gitlinks are written with plumbing, so no network or
# submodule objects are needed.
set -euo pipefail

guard="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reference-guard.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

ALERTA=f4312727783dad64198b62ed778bbca98a5144a3
WEBUI=2687e4afa93b41c4fa1d46201bba9ea38be0d491
OTHER=1111111111111111111111111111111111111111

# printf %b escapes, so values keep their trailing newline.
GM='[submodule "reference/alerta"]\n\tpath = reference/alerta\n\turl = https://github.com/alerta/alerta.git\n[submodule "reference/alerta-webui"]\n\tpath = reference/alerta-webui\n\turl = https://github.com/alerta/alerta-webui.git\n'

new_repo() {
  local dir="$work/$1"
  git init -q "$dir"
  git -C "$dir" config user.name t
  git -C "$dir" config user.email t@example.invalid
  printf '# alrust\n' > "$dir/README.md"
  git -C "$dir" add README.md
  git -C "$dir" commit -q -m base
  echo "$dir"
}

add_blob() { # repo path mode content
  local sha
  sha="$(printf '%b' "$4" | git -C "$1" hash-object -w --stdin)"
  git -C "$1" update-index --add --cacheinfo "$3,$sha,$2"
}

add_link() { git -C "$1" update-index --add --cacheinfo "160000,$3,$2"; }

scaffold() { # repo [alerta_sha] [gitmodules_content]
  add_blob "$1" .gitmodules 100644 "${3:-$GM}"
  add_link "$1" reference/alerta "${2:-$ALERTA}"
  add_link "$1" reference/alerta-webui "$WEBUI"
}

commit() { git -C "$1" commit -q -m "$2"; git -C "$1" rev-parse HEAD; }

pass=0; bad=0
expect() { # name want(pass|fail) repo head base
  local got=pass
  (cd "$3" && "$guard" "$4" "$5" >/dev/null 2>&1) || got=fail
  if [[ "$got" == "$2" ]]; then pass=$((pass + 1)); echo "ok   $1 ($got)"
  else bad=$((bad + 1)); echo "FAIL $1: expected $2, got $got"; fi
}

# 1. legitimate initial scaffold on a base without submodules
r="$(new_repo c1)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r"; h="$(commit "$r" s)"
expect "initial scaffold" pass "$r" "$h" "$b"

# 2. later change that does not touch reference/
add_blob "$r" docs/x.md 100644 'x\n'; h2="$(commit "$r" docs)"
expect "unrelated change after scaffold" pass "$r" "$h2" "$h"

# 3. gitlink bumped after scaffold
add_link "$r" reference/alerta "$OTHER"; h3="$(commit "$r" bump)"
expect "gitlink change after scaffold" fail "$r" "$h3" "$h2"

# 4. initial scaffold with a wrong gitlink
r="$(new_repo c4)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r" "$OTHER"; h="$(commit "$r" s)"
expect "initial scaffold wrong gitlink" fail "$r" "$h" "$b"

# 5. extra file under reference/
r="$(new_repo c5)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r"; add_blob "$r" reference/probe.txt 100644 'p\n'; h="$(commit "$r" s)"
expect "extra reference file" fail "$r" "$h" "$b"

# 6. extra .gitmodules setting
r="$(new_repo c6)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r" "$ALERTA" "${GM}\tupdate = none\n"; h="$(commit "$r" s)"
expect "extra .gitmodules setting" fail "$r" "$h" "$b"

# 7. .gitmodules with path/url pairing swapped
swapped='[submodule "reference/alerta"]\n\tpath = reference/alerta\n\turl = https://github.com/alerta/alerta-webui.git\n[submodule "reference/alerta-webui"]\n\tpath = reference/alerta-webui\n\turl = https://github.com/alerta/alerta.git\n'
r="$(new_repo c7)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r" "$ALERTA" "$swapped"; h="$(commit "$r" s)"
expect "swapped submodule urls" fail "$r" "$h" "$b"

# 8. gitlink replaced by a regular file at the same path
r="$(new_repo c8)"; b="$(git -C "$r" rev-parse HEAD)"
add_blob "$r" .gitmodules 100644 "$GM"; add_blob "$r" reference/alerta 100644 "$ALERTA\n"; add_link "$r" reference/alerta-webui "$WEBUI"; h="$(commit "$r" s)"
expect "gitlink replaced by blob" fail "$r" "$h" "$b"

# 9. .gitmodules as a symlink
# (the index refuses this, so build the tree with plumbing as a push could)
r="$(new_repo c9)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r"; t="$(git -C "$r" write-tree)"
t="$(git -C "$r" ls-tree "$t" | sed "s/^100644 blob [0-9a-f]*\t\.gitmodules$/120000 blob $(printf 'x' | git -C "$r" hash-object -w --stdin)\t.gitmodules/" | git -C "$r" mktree)"
h="$(git -C "$r" commit-tree "$t" -p "$b" -m s)"
expect ".gitmodules symlink" fail "$r" "$h" "$b"

# 10. extra gitlink elsewhere in the repository
r="$(new_repo c10)"; b="$(git -C "$r" rev-parse HEAD)"; scaffold "$r"; add_link "$r" vendor/x "$OTHER"; h="$(commit "$r" s)"
expect "extra gitlink outside reference/" fail "$r" "$h" "$b"

# 11. reference/ and .gitmodules removed
r="$(new_repo c11)"; b="$(git -C "$r" rev-parse HEAD)"; add_blob "$r" docs/x.md 100644 'x\n'; h="$(commit "$r" s)"
expect "no reference tree at all" fail "$r" "$h" "$b"

# 12. base commit not present: must fail closed, not skip the comparison
r="$(new_repo c12)"; scaffold "$r"; h="$(commit "$r" s)"
expect "unavailable base commit" fail "$r" "$h" "$OTHER"

echo "passed $pass, failed $bad"
[[ "$bad" -eq 0 ]]
