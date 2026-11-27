FROM docker.io/library/debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
ARG USER_UID=1000
ARG USER_GID=1000
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends openssh-client python3 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g "$USER_GID" sshaccess && useradd -m -u "$USER_UID" -g "$USER_GID" sshaccess \
    && mkdir -p /private /run/ssh-access \
    && chown sshaccess:sshaccess /private /run/ssh-access \
    && chmod 700 /private /run/ssh-access
COPY service.py unlock.py key.py /opt/ssh-access/
ENV SSH_AUTH_SOCK=/run/ssh-access/agent.sock
USER sshaccess
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/ssh-access/service.py"]
