#!/usr/bin/env bash
# Builds the two release artifacts Dockerfile.release is made from:
#   skeleton.tar.gz  every workspace package.json — enough for the production install
#   bundle.tar.gz    the built backend, with the frontend inside it
# scripts/artifactory.sh uploads them under a version.
#
#   scripts/build-release-artifacts.sh [out-dir]      # default dist-release/
#
# Runs on the build machine (a CI runner), not in the image: the 2,800-package
# dev install, tsc and the bundler need tools the curated runtime images lack.
# Yarn comes from the repo (.yarnrc.yml yarnPath), so no global yarn is needed.
set -euo pipefail
out="${1:-dist-release}"
cd "$(dirname "$0")/.."
yarn() { node .yarn/releases/yarn-*.cjs "$@"; }

yarn install --immutable
yarn tsc
yarn build:backend

mkdir -p "$out"
cp packages/backend/dist/skeleton.tar.gz packages/backend/dist/bundle.tar.gz "$out/"
ls -l "$out"
