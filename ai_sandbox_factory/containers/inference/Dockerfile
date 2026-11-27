FROM docker.io/library/node:24-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=2026-11-27
ARG NPM_VERSION=11.21.3
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --engine-strict "npm@${NPM_VERSION}" && npm cache clean --force
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY broker.mjs credentials.mjs ./
USER node
CMD ["node", "/app/broker.mjs", "/config/routes.json", "/private/credentials.json", "/run/inference/proxy.sock"]
