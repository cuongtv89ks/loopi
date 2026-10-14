#!/usr/bin/env bash
# Fail if a fragment line did not survive into the final .config (renamed symbol, unmet
# dependency or typo). "is not set" lines only fail if the symbol ended up enabled.
set -euo pipefail
config="$1"; shift
missing=0
for fragment in "$@"; do
  while IFS= read -r line; do
    if [[ "$line" =~ ^\#\ (BR2_[A-Z0-9_]+)\ is\ not\ set$ ]]; then
      if grep -q "^${BASH_REMATCH[1]}=y" "$config"; then
        echo "STILL ENABLED: ${BASH_REMATCH[1]} ($(basename "$fragment"))"; missing=1
      fi
    elif [[ "$line" =~ ^BR2_[A-Z0-9_]+= ]]; then
      if ! grep -Fqx -- "$line" "$config"; then
        echo "MISSING: $line ($(basename "$fragment"))"; missing=1
      fi
    fi
  done < "$fragment"
done
[ "$missing" = 0 ] && echo "verify-config: all fragment lines applied"
exit "$missing"
