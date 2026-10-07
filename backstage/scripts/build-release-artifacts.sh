#!/usr/bin/env bash
# Builds the two release artifacts Dockerfile.release downloads:
#   <tag>-skeleton.tar.gz  every package.json, for the production install
#   <tag>-bundle.tar.gz    the built backend, with the frontend inside it
# Upload both to the artifact store under the same tag.
#
#   scripts/build-release-artifacts.sh <release-tag> [out-dir]
set -euo pipefail
tag="${1:?usage: build-release-artifacts.sh <release-tag> [out-dir]}"
out="${2:-dist-release}"
cd "$(dirname "$0")/.."

yarn install --immutable
yarn tsc
yarn build:backend

mkdir -p "$out"
cp packages/backend/dist/skeleton.tar.gz "$out/$tag-skeleton.tar.gz"
cp packages/backend/dist/bundle.tar.gz "$out/$tag-bundle.tar.gz"
ls -l "$out"
