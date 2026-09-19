#!/usr/bin/env bash
# the checkout is shallow and its submodules carry no tags, while a dependency
# selected by `version` verifies against the release tag on its pinned commit,
# read from the checkout's own refs. fetch that one tag for every submodule
set -euo pipefail

for dep in dep/*/; do
    sha="$(git -C "$dep" rev-parse HEAD)"
    tag="$(git -C "$dep" ls-remote --tags origin \
        | awk -v sha="$sha" '$1 == sha && $2 ~ /\^\{\}$/ && !found { sub("refs/tags/", "", $2); sub(/\^\{\}$/, "", $2); print $2; found = 1 }')"
    if [ -n "$tag" ]; then
        git -C "$dep" fetch --depth=1 --no-tags origin "tag" "$tag"
        echo "setup: $dep is release $tag"
    else
        echo "setup: $dep carries no release tag"
    fi
done
