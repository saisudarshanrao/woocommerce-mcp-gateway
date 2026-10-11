#!/usr/bin/env bash
# Builds the image in <source directory> and pushes it as
# <repository>:<content tag>, where the tag is a hash of the directory (see
# image-tag.sh). Does nothing when the registry already has that tag.
#
# Set PLATFORM to build for a machine unlike this one, for example
# PLATFORM=linux/amd64 when building on an Apple Silicon Mac for an x86 VPS.
#
# Two optional variables cover an image that is not simply "the Dockerfile in
# this directory": IMAGE_TAG replaces the computed tag, and BUILD_ARGS is added
# to the docker build command (a --target, a --build-context).
set -euo pipefail

dir="${1:?usage: build-image.sh <source directory> <repository>}"
repository="${2:?usage: build-image.sh <source directory> <repository>}"
root="$(cd "$(dirname "$0")/.." && pwd)"

ref="${repository}:${IMAGE_TAG:-$("${root}/scripts/image-tag.sh" "$dir")}"

build() {
  # shellcheck disable=SC2086
  if [ -n "${PLATFORM:-}" ]; then
    docker buildx build --platform "$PLATFORM" ${BUILD_ARGS:-} --tag "$ref" "$@" "$dir"
  else
    docker build ${BUILD_ARGS:-} --tag "$ref" "$dir"
  fi
}

# Without a registry: copy the image straight onto a single k3s server over SSH
# and load it into the container runtime there. Kubernetes then finds it
# locally, because every image here has a tag that names its exact content and
# the charts never ask for a fresh pull. Set SHIP_TO to an SSH destination, and
# SSH_OPTS to any extra ssh options.
if [ -n "${SHIP_TO:-}" ]; then
  # shellcheck disable=SC2086
  remote() { ssh ${SSH_OPTS:-} "$SHIP_TO" "$@"; }
  if remote "sudo k3s ctr images ls -q" 2>/dev/null | grep -qxF "$ref"; then
    echo "${ref} is already on ${SHIP_TO}"
    exit 0
  fi
  if [ -n "${PLATFORM:-}" ]; then
    # Always rebuilt: an image of this tag may already be here, built for this
    # machine's CPU rather than the server's. The tag names the content, not
    # the CPU it was compiled for.
    build --load
  elif ! docker image inspect "$ref" >/dev/null 2>&1; then
    build
  fi
  echo "copying ${ref} to ${SHIP_TO}"
  docker save "$ref" | gzip | remote "gunzip | sudo k3s ctr images import -" >/dev/null
  exit 0
fi

if docker manifest inspect --insecure "$ref" >/dev/null 2>&1; then
  echo "${ref} is already in the registry"
  exit 0
fi
if [ -n "${PLATFORM:-}" ]; then
  build --push
else
  build
  docker push "$ref"
fi
