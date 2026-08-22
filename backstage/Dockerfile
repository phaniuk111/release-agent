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

EXPOSE 7007

CMD ["node", "packages/backend", "--config", "app-config.yaml", "--config", "app-config.docker.yaml"]
