FROM docker.io/library/node:24-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY broker.mjs credentials.mjs ./
USER node
CMD ["node", "/app/broker.mjs", "/config/routes.json", "/private/credentials.json", "/run/inference/proxy.sock"]
