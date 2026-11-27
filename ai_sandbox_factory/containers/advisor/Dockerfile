FROM docker.io/library/node:22-trixie-slim@sha256:f0960f8dc047f358e3fb428f36ccba21732194fa5f0cac849f401e63e62fb9e6
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
ARG USER_UID=1000
ARG USER_GID=1000
ARG NPM_VERSION=11.21.3
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    ca-certificates curl bubblewrap socat tini dbus-daemon gnome-keyring libsecret-tools xdg-dbus-proxy python3 \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi
COPY primary/agent-versions.json primary/install-agent.py /opt/agent-install/
RUN npm install --global --engine-strict "npm@${NPM_VERSION}" && npm cache clean --force \
    && python3 /opt/agent-install/install-agent.py codex /usr/local/bin/advisor-codex \
    && python3 /opt/agent-install/install-agent.py claude /usr/local/bin/advisor-claude \
    && python3 /opt/agent-install/install-agent.py agy /usr/local/bin/agy
COPY --from=pi-advisor / /opt/advisor/
COPY advisor/login.sh advisor/entrypoint.sh /opt/advisor/
RUN mkdir -p /work /run/advisor /run/egress /run/advisor-bus \
    && chown -R node:node /home/node /work /run/advisor /run/advisor-bus \
    && chmod 700 /run/advisor /run/advisor-bus \
    && chmod 755 /opt/advisor/*.sh
ENV HOME=/home/node CODEX_HOME=/home/node/.codex CLAUDE_CONFIG_DIR=/home/node/.claude DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/advisor-bus/bus \
    XDG_RUNTIME_DIR=/run/advisor-bus AGY_CLI_DISABLE_AUTO_UPDATE=true
USER node
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/advisor/entrypoint.sh"]
