#!/usr/bin/env bash
# Prints a tag derived from the contents of an image's source directory, so the
# tag changes exactly when the image would. Because a given tag always means the
# same content, the cluster can keep imagePullPolicy: IfNotPresent, and a
# rebuild triggers a rollout only when something actually changed.
#
# Inside a git checkout only files git would track are hashed, which leaves out
# virtualenvs, caches and anything else .gitignore names. Documentation and
# tests are left out too: they are never copied into an image, so editing a
# README should not roll out a new pod.
set -euo pipefail

dir="${1:?usage: image-tag.sh <image source directory>}"
if command -v sha256sum >/dev/null 2>&1; then sum="sha256sum"; else sum="shasum -a 256"; fi

cd "$dir"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  files() { git ls-files -z --cached --others --exclude-standard . | tr '\0' '\n' | sed 's|^|./|'; }
else
  files() { find . -type f; }
fi

# shellcheck disable=SC2086
files | grep -vE '(^|/)tests/|\.md$' | LC_ALL=C sort | tr '\n' '\0' | xargs -0 $sum | $sum | cut -c1-12
