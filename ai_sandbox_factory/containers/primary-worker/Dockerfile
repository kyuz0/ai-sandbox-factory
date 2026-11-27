FROM docker.io/library/node:24-trixie-slim AS worker
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=2026-11-27
ARG USER_UID=1000
ARG USER_GID=1000
ARG NPM_VERSION=11.21.3
COPY --from=primary-dev cli-tools/ /opt/cli-tools/
RUN sh /opt/cli-tools/install.sh \
    && npm install --global --engine-strict "npm@${NPM_VERSION}" && npm cache clean --force \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi \
    && chown -R node:node /home/node \
    && install -d -m 0700 -o node -g node /run/primary-workspace \
    && install -d -o node -g node /workspace \
    && printf 'Include /etc/sandbox-ssh/config\n' >/etc/ssh/ssh_config.d/10-sandbox.conf
COPY sandbox-update.py shell-network.sh /opt/sandbox/
RUN ln -s /run/primary-runtime/sandbox-context.md /opt/sandbox/context.md
RUN printf '\n. /opt/sandbox/shell-network.sh\n' >> /etc/bash.bashrc \
    && printf '. /opt/sandbox/shell-network.sh\n' > /etc/profile.d/factory-network.sh
COPY entrypoint.py /opt/primary/entrypoint.py
COPY --from=primary-files worker.sh worker-tools.mjs /opt/primary/
ENV GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=include.path GIT_CONFIG_VALUE_0=/opt/sandbox/git-identity.gitconfig
ENV BASH_ENV=/opt/sandbox/shell-network.sh
USER node
WORKDIR /workspace
RUN python3 /opt/cli-tools/check.py
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/primary/entrypoint.py"]
