FROM docker.io/library/node:24-bookworm-slim AS worker
ARG USER_UID=1000
ARG USER_GID=1000
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates curl fd-find file git jq less nano netcat-openbsd \
    openssh-client patch procps python3 ripgrep rsync socat tini tmux unzip vim-tiny wget zip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi \
    && chown -R node:node /home/node \
    && install -d -m 0700 -o node -g node /run/primary-workspace \
    && install -d -o node -g node /workspace \
    && printf 'Include /etc/sandbox-ssh/config\n' >/etc/ssh/ssh_config.d/10-sandbox.conf
COPY entrypoint.py /opt/primary/entrypoint.py
COPY --from=primary-files worker.sh worker-tools.mjs editor_client.py /opt/primary/
ENV GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=include.path GIT_CONFIG_VALUE_0=/opt/sandbox/git-identity.gitconfig
USER node
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/primary/entrypoint.py"]
