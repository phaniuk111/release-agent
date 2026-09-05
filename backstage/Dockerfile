# Backstage PoC Docker image (multi-stage, yarn 4 via corepack).
# Simplified for local PoC: we keep full node_modules in the runtime image
# instead of the pruned production bundle. Larger image, far simpler build.
FROM node:22-bookworm-slim

# node-gyp deps for native modules (isolated-vm, better-sqlite3)
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 g++ make \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHON=/usr/bin/python3
WORKDIR /app

RUN corepack enable

# Copy the whole workspace (respects .dockerignore)
COPY . .

RUN yarn install --immutable
RUN yarn tsc && yarn build:backend

# yarn build:backend emits dist/bundle.tar.gz; unpack it so
# packages/backend/dist/index.cjs.js exists for `node packages/backend`.
WORKDIR /app
# bundle.tar.gz contains the full workspace tree (packages/backend/dist etc.)
# and is meant to be extracted over /app, per the official Backstage Dockerfile.
RUN tar -xzf packages/backend/dist/bundle.tar.gz \
  && rm -f packages/backend/dist/bundle.tar.gz packages/backend/dist/skeleton.tar.gz

# SQLite data dir (app-config.docker.yaml points the DB here). app.docker
# runs without a volume: create the dir now, own it as uid 1000 so the
# k8s podSecurityContext (runAsUser/fsGroup 1000) can write it too.
RUN mkdir -p /app/backstage-data && chown -R 1000:1000 /app/backstage-data

# WORKDIR creates /app as root with mode 0700, and everything the app needs to
# READ inside it is already world-readable — so the one thing standing between
# uid 1000 and the app is the traverse bit on /app itself. Without this the
# container dies with a MODULE_NOT_FOUND for /app/packages/backend that looks
# like a broken build rather than a permissions problem. `chmod` not
# `chown -R`: recursing over node_modules would add a ~1.5GB layer.
RUN chmod 755 /app

# Run as non-root by default so the image matches the Helm chart's
# podSecurityContext instead of depending on it.
USER 1000

EXPOSE 7007

CMD ["node", "packages/backend", "--config", "app-config.yaml", "--config", "app-config.docker.yaml"]
