#!/usr/bin/env bash
set -euo pipefail
. .env
. version.sh

LOCAL_IMAGE="localhost/${IMAGE}:${FULL_VERSION}"

podman build \
    --build-arg "BUILD_NUMBER=${BUILD_NUMBER}" \
    -t "$LOCAL_IMAGE" \
    -f Containerfile .
