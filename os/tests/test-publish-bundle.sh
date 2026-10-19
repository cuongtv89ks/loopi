#!/usr/bin/env bash
# Host test of os/scripts/publish-bundle.sh against a throwaway git repository. Usage: os/tests/test-publish-bundle.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLISH="$HERE/../scripts/publish-bundle.sh"
fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
REPO="$W/repo"; OTA="$W/ota"; OUT="$W/out"
mkdir -p "$REPO" "$OTA" "$OUT"
g() { git -C "$REPO" -c user.name=t -c user.email=t@t "$@"; }
commit() { mkdir -p "$REPO/$(dirname "$1")"; echo "$RANDOM" >> "$REPO/$1"; g add -A; g commit -q -m "$2"; }
g init -q
commit agent/a "feat(agent): one"
commit brain/b "feat(brain): not a device change"
commit os/x "feat(os): two"
commit docs/protocol/p "feat(protocol): three"
build() {  # build <variant> <version> <built_at>: a fake os/out bundle for HEAD
  local name="loopi-$1-$2"
  printf 'bundle %s' "$2" > "$OUT/$name.raucb"
  local sha size
  sha="$(sha256sum "$OUT/$name.raucb" | cut -d' ' -f1)"; size="$(stat -c %s "$OUT/$name.raucb")"
  printf '{"bundle": "%s.raucb", "version": "%s", "variant": "%s", "sha256": "%s", "size": %s, "built_at": "%s", "commit": "%s"}\n' \
    "$name" "$2" "$1" "$sha" "$size" "$3" "$(g rev-parse HEAD)" > "$OUT/$name.json"
  echo "$OUT/$name.json"
}
publish() { LOOPI_REPO="$REPO" "$PUBLISH" "$@"; }
notes() { jq -r '.notes | map(.subject) | join("|")' "$OTA/$1.json"; }

first="$(build dev 1.0 2026-10-20T01:00:00Z)"
check "first bundle publishes" publish "$first" "$OTA"
check "bundle copied" cmp -s "$OUT/loopi-dev-1.0.raucb" "$OTA/loopi-dev-1.0.raucb"
check "first notes: device commits only, oldest first" test "$(notes loopi-dev-1.0)" = "feat(agent): one|feat(os): two|feat(protocol): three"
check "first notes have no base" test "$(jq -r .notes_base "$OTA/loopi-dev-1.0.json")" = null
check "notes keep the full commit" test "$(jq -r '.notes[0].commit | length' "$OTA/loopi-dev-1.0.json")" = 40
check "description keeps the build fields" test "$(jq -r .size "$OTA/loopi-dev-1.0.json")" = "$(stat -c %s "$OUT/loopi-dev-1.0.raucb")"
check "publishing twice is refused" bash -c "! LOOPI_REPO='$REPO' '$PUBLISH' '$first' '$OTA'"

commit agent/a "fix(agent): four"
commit brain/b "fix(brain): five"
second="$(build prod 1.1 2026-10-21T01:00:00Z)"
check "second bundle publishes" publish "$second" "$OTA"
check "second notes: only what changed since the newest published (any variant)" test "$(notes loopi-prod-1.1)" = "fix(agent): four"
check "second notes name their base" test "$(jq -r .notes_base "$OTA/loopi-prod-1.1.json")" = 1.0
check "not truncated" test "$(jq -r .notes_truncated "$OTA/loopi-prod-1.1.json")" = false

for i in $(seq 1 55); do commit agent/a "feat(agent): change $i"; done
many="$(build dev 1.2 2026-10-22T01:00:00Z)"
check "many changes publish" publish "$many" "$OTA"
check "notes stop at 50" test "$(jq '.notes | length' "$OTA/loopi-dev-1.2.json")" = 50
check "the 50 newest are kept, newest last" test "$(jq -r '.notes[-1].subject' "$OTA/loopi-dev-1.2.json")" = "feat(agent): change 55"
check "truncation is said" test "$(jq -r .notes_truncated "$OTA/loopi-dev-1.2.json")" = true

echo '{not json' > "$OTA/loopi-dev-junk.json"
commit os/x "feat(os): six"
fifth="$(build dev 1.3 2026-10-23T01:00:00Z)"
check "an unreadable description in the bundle dir is ignored" publish "$fifth" "$OTA"
check "base is still the newest valid one" test "$(jq -r .notes_base "$OTA/loopi-dev-1.3.json")" = 1.2

dirty="$(build dev 1.4-dirty 2026-10-24T01:00:00Z)"
check "dirty builds are refused" bash -c "! LOOPI_REPO='$REPO' '$PUBLISH' '$dirty' '$OTA'"
broken="$(build dev 1.4-broken-agent 2026-10-24T01:00:00Z)"
check "broken acceptance builds need --acceptance" bash -c "! LOOPI_REPO='$REPO' '$PUBLISH' '$broken' '$OTA'"
check "--acceptance allows them" publish --acceptance "$broken" "$OTA"
tampered="$(build dev 1.5 2026-10-25T01:00:00Z)"; echo x >> "$OUT/loopi-dev-1.5.raucb"
check "a bundle that does not match its description is refused" bash -c "! LOOPI_REPO='$REPO' '$PUBLISH' '$tampered' '$OTA'"
lost="$(build dev 1.6 2026-10-26T01:00:00Z)"; jq '.commit = "0000000000000000000000000000000000000000"' "$lost" > "$W/l" && mv "$W/l" "$lost"
check "an unknown commit is refused" bash -c "! LOOPI_REPO='$REPO' '$PUBLISH' '$lost' '$OTA'"
check "no temporary files left" bash -c "! ls -A '$OTA' | grep -q '^\.'"
exit "$fail"
