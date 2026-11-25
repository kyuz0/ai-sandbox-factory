FROM docker.io/library/node:24-trixie-slim AS worker
ARG USER_UID=1000
ARG USER_GID=1000
COPY --from=primary-dev cli-tools/ /opt/cli-tools/
RUN sh /opt/cli-tools/install.sh \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi \
    && chown -R node:node /home/node \
    && install -d -m 0700 -o node -g node /run/primary-workspace \
    && install -d -o node -g node /workspace \
    && printf 'Include /etc/sandbox-ssh/config\n' >/etc/ssh/ssh_config.d/10-sandbox.conf
COPY sandbox-update.py shell-network.sh /opt/sandbox/
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
