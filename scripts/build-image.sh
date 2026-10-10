#!/usr/bin/env bash
# Builds the image in <source directory> and pushes it as
# <repository>:<content tag>, where the tag is a hash of the directory (see
# image-tag.sh). Does nothing when the registry already has that tag.
#
# Set PLATFORM to build for a machine unlike this one, for example
# PLATFORM=linux/amd64 when building on an Apple Silicon Mac for an x86 VPS.
set -euo pipefail

dir="${1:?usage: build-image.sh <source directory> <repository>}"
repository="${2:?usage: build-image.sh <source directory> <repository>}"
root="$(cd "$(dirname "$0")/.." && pwd)"

ref="${repository}:$("${root}/scripts/image-tag.sh" "$dir")"

if docker manifest inspect --insecure "$ref" >/dev/null 2>&1; then
  echo "${ref} is already in the registry"
  exit 0
fi
if [ -n "${PLATFORM:-}" ]; then
  docker buildx build --platform "$PLATFORM" --tag "$ref" --push "$dir"
else
  docker build --tag "$ref" "$dir"
  docker push "$ref"
fi
