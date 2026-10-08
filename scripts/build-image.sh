#!/usr/bin/env bash
# Builds the image in <source directory> and pushes it as
# <repository>:<content tag>, where the tag is a hash of the directory (see
# image-tag.sh). Does nothing when the registry already has that tag.
set -euo pipefail

dir="${1:?usage: build-image.sh <source directory> <repository>}"
repository="${2:?usage: build-image.sh <source directory> <repository>}"
root="$(cd "$(dirname "$0")/.." && pwd)"

ref="${repository}:$("${root}/scripts/image-tag.sh" "$dir")"

if docker manifest inspect --insecure "$ref" >/dev/null 2>&1; then
  echo "${ref} is already in the registry"
  exit 0
fi
docker build --tag "$ref" "$dir"
docker push "$ref"
