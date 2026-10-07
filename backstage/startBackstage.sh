#!/bin/bash
# Entry point of the release image (Dockerfile.release).
#
# app-config.yaml always; APP_CONFIG_EXTRA adds more, in order, last one wins —
# e.g. APP_CONFIG_EXTRA="app-config.docker.yaml app-config.kubernetes.yaml".
# `exec` hands the process to Node, so the pod's SIGTERM reaches Backstage and
# it shuts down cleanly instead of being killed when the grace period ends.
set -u
args=(--config app-config.yaml)
for f in ${APP_CONFIG_EXTRA:-}; do
  args+=(--config "$f")
done
exec node packages/backend "${args[@]}"
