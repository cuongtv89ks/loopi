#!/usr/bin/env bash
# Publish a built OTA bundle to the brain (P4a design §8.1, spec D29): add release notes from git, then copy the bundle
# and its description into the brain's bundle directory (LOOPI_OTA_DIR; /srv/loopi/ota in the api container).
# Usage: os/scripts/publish-bundle.sh [--acceptance] <os/out/loopi-<variant>-<version>.json> <ota dir>
#   --acceptance  also allows the deliberately broken acceptance bundles (version contains -broken-)
# The notes are the subjects of the commits touching agent/, os/ or docs/protocol/ since the newest bundle already in
# the directory (any variant: both are built from the same code), at most 50 (the newest); with no usable base, the
# last 20.
set -euo pipefail
MAX_NOTES=50
FIRST_NOTES=20
NOTE_PATHS=(agent os docs/protocol)
REPO="${LOOPI_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
die() { echo "publish-bundle: $*" >&2; exit 1; }

acceptance=0
if [ "${1:-}" = --acceptance ]; then acceptance=1; shift; fi
[ $# = 2 ] || { echo "usage: $0 [--acceptance] <bundle.json> <ota dir>" >&2; exit 2; }
desc="$1"
ota="$2"
[ -f "$desc" ] || die "$desc not found"
[ -d "$ota" ] || die "$ota is not a directory"
bundle="$(jq -er .bundle "$desc")" || die "$desc has no bundle name"
version="$(jq -er .version "$desc")" || die "$desc has no version"
commit="$(jq -er .commit "$desc")" || die "$desc has no commit"
sha="$(jq -er .sha256 "$desc")" || die "$desc has no sha256"
size="$(jq -er .size "$desc")" || die "$desc has no size"
desc_name="$(basename "$desc")"
[ "${desc_name%.json}.raucb" = "$bundle" ] || die "$desc does not describe $bundle"
src="$(dirname "$desc")/$bundle"
[ -f "$src" ] || die "$src not found"
case "$version" in *-dirty*) die "$version was built from uncommitted changes; commit them and build again" ;; esac
case "$version" in
  *-broken-*) [ "$acceptance" = 1 ] || die "$version is a deliberately broken acceptance bundle; pass --acceptance" ;;
esac
[ ! -e "$ota/$bundle" ] && [ ! -e "$ota/$desc_name" ] || die "$bundle is already published"
git -C "$REPO" cat-file -e "$commit^{commit}" 2>/dev/null || die "commit $commit is not in $REPO"
[ "$(stat -c %s "$src")" = "$size" ] || die "$bundle is not $size bytes"
[ "$(sha256sum "$src" | cut -d' ' -f1)" = "$sha" ] || die "$bundle does not match its sha256"

# The newest valid description already published, if its commit is an ancestor of this one. "Valid" here means that
# its version and commit are what the brain accepts again as notes_base and what git can resolve.
entries=""
for f in "$ota"/loopi-*.json; do
  [ -f "$f" ] || continue
  entry="$(jq -c 'select(type == "object" and (.built_at|type) == "string" and (.version|type) == "string"
                  and (.commit|type) == "string"
                  and (.version|test("^[0-9A-Za-z.+-]{1,64}$")) and (.commit|test("^[0-9a-f]{40}$")))
                  | {built_at, version, commit}' "$f" 2>/dev/null)" || continue
  [ -n "$entry" ] && entries+="$entry"$'\n'
done
newest="$(printf '%s' "$entries" | jq -sc 'sort_by(.built_at) | last // empty')"
base_version=null
base_commit=""
if [ -n "$newest" ]; then
  candidate="$(jq -r .commit <<<"$newest")"
  if git -C "$REPO" cat-file -e "$candidate^{commit}" 2>/dev/null \
      && git -C "$REPO" merge-base --is-ancestor "$candidate" "$commit"; then
    base_version="$(jq -c .version <<<"$newest")"
    base_commit="$candidate"
  fi
fi
if [ -n "$base_commit" ]; then
  log="$(git -C "$REPO" log --reverse --format='%H%x09%s' "$base_commit..$commit" -- "${NOTE_PATHS[@]}")"
else
  log="$(git -C "$REPO" log --reverse --format='%H%x09%s' -n "$FIRST_NOTES" "$commit" -- "${NOTE_PATHS[@]}")"
fi
count="$(grep -c . <<<"$log" || true)"
truncated=false
if [ "$count" -gt "$MAX_NOTES" ]; then
  log="$(tail -n "$MAX_NOTES" <<<"$log")"
  truncated=true
fi
notes="$(jq -R -s -c 'split("\n") | map(select(length > 0) | split("\t") | {commit: .[0], subject: (.[1:] | join("\t"))})' <<<"$log")"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"; rm -f "$ota/.$bundle.tmp" "$ota/.$desc_name.tmp"' EXIT
jq --argjson notes "$notes" --argjson base "$base_version" --argjson truncated "$truncated" \
  '. + {notes_base: $base, notes: $notes, notes_truncated: $truncated}' "$desc" > "$tmp/desc.json"
# The bundle first: the brain lists only bundles that have a description.
cp "$src" "$ota/.$bundle.tmp" && mv "$ota/.$bundle.tmp" "$ota/$bundle"
cp "$tmp/desc.json" "$ota/.$desc_name.tmp" && mv "$ota/.$desc_name.tmp" "$ota/$desc_name"
echo "published $bundle ($(jq length <<<"$notes") notes$([ "$truncated" = true ] && echo ", truncated"))"
