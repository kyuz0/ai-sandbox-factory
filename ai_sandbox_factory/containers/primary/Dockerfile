FROM docker.io/library/node:24-trixie-slim
ARG AGENT
ARG USER_UID=1000
ARG USER_GID=1000
ARG CODEX_VERSION=0.171.0
ARG CLAUDE_VERSION=2.1.330
ARG AGY_BUILD=1.5.13-5999302598852608
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git tini socat python3 dbus-daemon gnome-keyring libsecret-tools \
    python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi
RUN set -eu; case "$AGENT" in \
    codex) python3 -m venv /opt/codex-proxy; /opt/codex-proxy/bin/pip install --no-cache-dir 'mitmproxy==12.3.1'; \
        npm install -g "@openai/codex@$CODEX_VERSION"; \
        find /usr/local/lib/node_modules/@openai -type f -name codex -executable \
            -exec install -m 755 '{}' /usr/local/bin/factory-agent ';'; \
        find /usr/local/lib/node_modules/@openai -type f -name codex-code-mode-host -executable \
            -exec install -m 755 '{}' /usr/local/bin/codex-code-mode-host ';'; \
        test -x /usr/local/bin/codex-code-mode-host ;; \
    claude) python3 -m venv /opt/claude-proxy; /opt/claude-proxy/bin/pip install --no-cache-dir 'mitmproxy==12.3.1'; \
        curl -fsSLo /tmp/install.sh https://claude.ai/install.sh; \
        bash /tmp/install.sh "$CLAUDE_VERSION"; \
        install -m 755 /root/.local/bin/claude /usr/local/bin/factory-agent; \
        rm -rf /root/.local/share/claude /root/.local/bin/claude /tmp/install.sh ;; \
    agy) python3 -m venv /opt/agy-proxy; /opt/agy-proxy/bin/pip install --no-cache-dir 'mitmproxy==12.3.1'; \
        case "$(uname -m)" in \
            x86_64) archive=linux-x64/cli_linux_x64.tar.gz; \
                checksum=014b37387703acab1f47cd532c8bb60167a6a3537b0f9891de03f1b1ffa9614307bc83c4b2f7551273b0877d722716b0cd9e26782f6b2a50705db4f05e18c68f ;; \
            aarch64) archive=linux-arm/cli_linux_arm64.tar.gz; \
                checksum=dccea2eb455099f5175f0c87cc47b38fcefe4cb0cb44dea3927469bb4bbf68ac3c0b30fc04affb24c17e90959386a93c3785e8e4b8b1a3338ca4e4d57b80057b ;; \
            *) echo 'Antigravity CLI requires a 64-bit x86 or Arm container'; exit 2 ;; \
        esac; \
        curl -fsSLo /tmp/agy.tar.gz "https://storage.googleapis.com/antigravity-public/antigravity-cli/$AGY_BUILD/$archive"; \
        echo "$checksum  /tmp/agy.tar.gz" | sha512sum -c -; \
        tar -xzf /tmp/agy.tar.gz -C /tmp antigravity; \
        install -m 755 /tmp/antigravity /usr/local/bin/factory-agent; \
        rm /tmp/agy.tar.gz /tmp/antigravity ;; \
    *) exit 2 ;; esac; /usr/local/bin/factory-agent --version
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
