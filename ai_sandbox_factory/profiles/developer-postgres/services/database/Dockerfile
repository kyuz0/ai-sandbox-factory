FROM docker.io/library/postgres:17-alpine
RUN apk add --no-cache openssh-server \
    && mkdir -p /run/sshd \
    && sed -i 's/^root:[^:]*:/root::/' /etc/shadow
# Factory supplies the sandbox's authorized key and host key at runtime.
# The PostgreSQL entrypoint and command are inherited unchanged.
