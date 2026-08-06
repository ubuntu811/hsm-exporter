# Sourced by build.sh/run.sh - single place computing the full image version, so they
# can't independently drift. BASE_VERSION comes from pyproject.toml (MAJOR.MINOR,
# hand-maintained); BUILD_NUMBER is GitLab's CI_PIPELINE_IID once this runs in a
# pipeline, or "dev" for a local build outside CI.
BASE_VERSION=$(grep -m1 '^version = ' pyproject.toml | sed -E 's/version = "(.*)"/\1/')
BUILD_NUMBER="${CI_PIPELINE_IID:-dev}"
FULL_VERSION="${BASE_VERSION}.${BUILD_NUMBER}"
