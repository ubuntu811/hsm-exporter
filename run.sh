#!/usr/bin/env bash
set -euo pipefail

. .env

CONFIG_DIR="${CONFIG_DIR:-$(pwd)/config}"

chmod 0755 "${CONFIG_DIR}"
chmod 0644 "${CONFIG_DIR}/hsms.yml"

podman run --rm \
    --name luna-hsm-monitor \
    --env-file .env \
    -p 8080:8080 \
    -v "${CONFIG_DIR}:/config:ro,Z" \
    "$LOCAL_IMAGE"
