FROM docker.io/library/node:24-trixie-slim
ARG AGENT
ARG USER_UID=1000
ARG USER_GID=1000
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git tini socat python3 dbus-daemon gnome-keyring libsecret-tools \
    python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi
COPY agent-versions.json install-agent.py /opt/primary/
RUN python3 -m venv /opt/${AGENT}-proxy \
    && /opt/${AGENT}-proxy/bin/pip install --no-cache-dir 'mitmproxy==12.3.1' \
    && python3 /opt/primary/install-agent.py "$AGENT" /usr/local/bin/factory-agent
COPY entrypoint.sh /opt/primary/
COPY codex_auth.py codex_auth_proxy.py codex_connect.py codex_network.json /opt/primary/
COPY claude_auth.py claude_auth_proxy.py /opt/primary/
COPY agy_auth.py agy_auth_proxy.py agy_network.json /opt/primary/
RUN install -d -o node -g node -m 700 /work /workspace /run/primary-bus /run/primary-editor \
    && chmod 755 /opt/primary/entrypoint.sh
ENV HOME=/home/node CODEX_HOME=/home/node/.codex CLAUDE_CONFIG_DIR=/home/node/.claude \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/primary-bus/bus XDG_RUNTIME_DIR=/run/primary-bus \
    DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 AGY_CLI_DISABLE_AUTO_UPDATE=true
USER node
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/primary/entrypoint.sh"]
