FROM docker.io/library/node:24-trixie-slim@sha256:4d00acb4bcc42c454c4eb801526f7ad7f772ea3bd4fd168c5cc2e384abcbb9d4
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
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
