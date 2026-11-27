FROM docker.io/library/postgres:17-alpine@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73
RUN apk add --no-cache openssh-server \
    && mkdir -p /run/sshd \
    && sed -i 's/^root:[^:]*:/root::/' /etc/shadow
# Factory supplies the sandbox's authorized key and host key at runtime.
# The PostgreSQL entrypoint and command are inherited unchanged.
