#!/usr/bin/env bash
# Prints a tag derived from the contents of an image's source directory, so the
# tag changes exactly when the image would. Because a given tag always means the
# same content, the cluster can keep imagePullPolicy: IfNotPresent, and a
# rebuild triggers a rollout only when something actually changed.
set -euo pipefail

dir="${1:?usage: image-tag.sh <image source directory>}"
if command -v sha256sum >/dev/null 2>&1; then sum="sha256sum"; else sum="shasum -a 256"; fi

(cd "$dir" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 $sum) | $sum | cut -c1-12
