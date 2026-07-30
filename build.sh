#!/usr/bin/env bash
set -euo pipefail
. .env

podman build -t "$LOCAL_IMAGE" -f Containerfile .
