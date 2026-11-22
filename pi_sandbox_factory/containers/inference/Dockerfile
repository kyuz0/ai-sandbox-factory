FROM docker.io/library/node:24-trixie-slim@sha256:60587baa7c895cf438d44f691e4ac8248b36c13b26be77332f874bebb3a64201
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY broker.mjs credentials.mjs ./
USER node
CMD ["node", "/app/broker.mjs", "/config/routes.json", "/private/credentials.json", "/run/inference/proxy.sock"]
