FROM docker.io/library/node:24-bookworm-slim AS frostpi-build
WORKDIR /src
COPY --from=frostpi / ./
RUN npm exec --yes --package=pnpm@10.13.1 -- pnpm install --frozen-lockfile \
    && npm exec --yes --package=pnpm@10.13.1 -- pnpm package:vsix \
    && /bin/sh -c 'set -- artifacts/FrostPi-*.vsix; test "$#" = 1 && cp "$1" /frostpi.vsix'

FROM docker.io/library/node:24-bookworm-slim
ARG PI_VERSION=1.4.0
ARG USER_UID=1000
ARG USER_GID=1000
# Strip ping privileges so the executable can start with all container capabilities dropped.
# Keep manuals for packages installed here and restore the shell manuals stripped from node:slim.
RUN echo 'path-include=/usr/share/man/*' > /etc/dpkg/dpkg.cfg.d/zz-cli-manuals \
    && apt-get update \
    && apt-get install -y --no-install-recommends --reinstall \
    bash coreutils diffutils findutils grep gzip mawk sed tar util-linux \
    && apt-get install -y --no-install-recommends \
    bash-completion man-db manpages manpages-dev git-man \
    build-essential patch tree bsdextrautils xxd acl bc time gettext-base \
    zstd cpio lsof psmisc net-tools traceroute \
    ca-certificates curl wget git python3 socat tini procps ripgrep iproute2 iputils-ping \
    dnsutils netcat-openbsd jq less nano vim file zip unzip bzip2 xz-utils rsync tmux \
    && python3 -c 'import os; p="/usr/bin/ping"; os.removexattr(p, "security.capability") if "security.capability" in os.listxattr(p) else None' \
    && chmod u-s /usr/bin/ping \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$USER_GID" != 1000 ]; then groupmod -g "$USER_GID" node; fi \
    && if [ "$USER_UID" != 1000 ]; then usermod -u "$USER_UID" node; fi \
    && chown -R node:node /home/node
COPY document-packages.txt document-requirements.txt install-document-tools.sh /opt/document-tools/
RUN apt-get update \
    && xargs -r apt-get install -y --no-install-recommends < /opt/document-tools/document-packages.txt \
    && cp /opt/document-tools/document-requirements.txt /opt/document-tools/requirements.txt \
    && sh /opt/document-tools/install-document-tools.sh \
    && rm -rf /var/lib/apt/lists/*
COPY check-document-tools.py /opt/document-tools/check.py
COPY document-tools.md /opt/sandbox/document-tools.md
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp-dev meson ninja-build pkg-config libcap-dev libselinux1-dev \
    && rm -rf /var/lib/apt/lists/*
COPY security/ /opt/pi-security/
RUN python3 /opt/pi-security/fetch.py \
    && npm ci --omit=dev --prefix /opt/pi-security \
    && python3 /opt/pi-security/scripts/prepare-container.py
COPY compaction-recall/ /opt/pi-compaction-recall/
RUN npm ci --omit=dev --ignore-scripts --legacy-peer-deps --prefix /opt/pi-compaction-recall
ENV PI_SANDBOX_CONTAINER=1
RUN install -d -o node -g node -m 700 /var/tmp/pi
ENV TMPDIR=/var/tmp/pi TMUX_TMPDIR=/var/tmp/pi
COPY security/security.md /opt/sandbox/security.md
# Build the pinned source before opening the sandbox editor.
COPY --from=frostpi-build /frostpi.vsix /tmp/frostpi.vsix
COPY --from=frostpi-build /src/source-commit /opt/sandbox/frostpi-source-commit
RUN mkdir -p /opt/editor-extensions \
    && unzip -q /tmp/frostpi.vsix 'extension/*' -d /tmp/frostpi \
    && version="$(node -p 'require("/tmp/frostpi/extension/package.json").version')" \
    && mv /tmp/frostpi/extension "/opt/editor-extensions/frostime.frostpi-$version" \
    && rm /tmp/frostpi.vsix
COPY editor-startup/ /opt/editor-extensions/pi-sandbox-factory.editor-startup-1.1.0/
# The package has no runtime dependencies. Both clients resolve its peers from Pi.
RUN curl -fsSL --retry 3 https://registry.npmjs.org/@ethanthoma/pi-goal/-/pi-goal-0.2.1.tgz -o /tmp/pi-goal.tgz \
    && printf '%s  %s\n' d648ae641f0af9ef8b3931f52866769016ded4a023261ee378637357002a4053 /tmp/pi-goal.tgz | sha256sum -c - \
    && mkdir -p /opt/pi-goal \
    && tar -xzf /tmp/pi-goal.tgz --strip-components=1 -C /opt/pi-goal \
    && rm /tmp/pi-goal.tgz
COPY entrypoint.sh /usr/local/bin/sandbox-entrypoint
COPY --from=pi-local-model-manager / /opt/pi-local-model-manager/
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/ssh/ssh_config.d \
    && printf "Include /etc/sandbox-ssh/config\n" > /etc/ssh/ssh_config.d/10-sandbox.conf
# Pi uses --no-require-git when searching ordinary folders. Bookworm's fd lacks it.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) arch=x86_64; sha=7e2d07fa2b8807642aaec67b7e2972a9fbe4db26fcb340a7372392db42b26694 ;; \
        arm64) arch=aarch64; sha=63ea52cb31586e6d98efa2f06d73d542f16c7d0c4af0f07b579aaf9714cf907a ;; \
        i386) arch=i686; sha=98e61a903fd3e58f92bc268e37b2cd0668195b9383d696693a1fca0b39d5a1f0 ;; \
        *) echo "No bundled fd binary for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    version=10.5.2; asset="fd-v${version}-${arch}-unknown-linux-musl"; \
    curl -fsSL --retry 3 "https://github.com/sharkdp/fd/releases/download/v${version}/${asset}.tar.gz" -o /tmp/fd.tar.gz; \
    echo "$sha  /tmp/fd.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/fd.tar.gz -C /usr/local/bin --no-same-owner --strip-components=1 "$asset/fd"; \
    install -d /usr/local/share/man/man1; \
    tar -xOzf /tmp/fd.tar.gz "$asset/fd.1" > /usr/local/share/man/man1/fd.1; \
    rm /tmp/fd.tar.gz; \
    test "$(fd --no-require-git --glob fd /usr/local/bin)" = /usr/local/bin/fd
COPY --from=pi-advisor / /opt/pi-advisor/
COPY --from=rpiv-ask-user-question /packages/rpiv-ask-user-question/ /opt/rpiv-ask-user-question/
COPY --from=rpiv-ask-user-question /packages/rpiv-config/ /opt/rpiv-ask-user-question/node_modules/@juicesharp/rpiv-config/
COPY --from=rpiv-ask-user-question /source-commit /opt/rpiv-ask-user-question/source-commit
COPY --from=pi-session-naming /pi-packages/session-naming/config.ts /pi-packages/session-naming/generator.ts /LICENSE /source-commit /opt/pi-session-naming/
COPY --from=pi-session-naming /pi-packages/session-naming/extensions/index.ts /opt/pi-session-naming/extensions/
COPY session-naming.mjs /opt/pi-session-naming/factory.mjs
COPY system-prompt.mjs /opt/sandbox/system-prompt.mjs
COPY local-knowledge-status.mjs /opt/sandbox/local-knowledge-status.mjs
COPY profile-guide.mjs /opt/sandbox/profile-guide.mjs
RUN rm -rf /opt/pi-security/node_modules/pi-goal
COPY bootstrap.py /usr/local/lib/sandbox-bootstrap.py
COPY ssh-status.py /usr/local/bin/sandbox-ssh-status
COPY communications-config.py /usr/local/lib/sandbox-communications.py
COPY web-access-config.py /usr/local/lib/sandbox-web-access.py
RUN chmod 755 /usr/local/bin/sandbox-entrypoint /usr/local/bin/sandbox-ssh-status \
    && mkdir -p /workspace /pi-config /run/egress \
    && chown node:node /workspace
ENV PI_CODING_AGENT_DIR=/home/node/.pi/agent
USER node
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sandbox-entrypoint"]
