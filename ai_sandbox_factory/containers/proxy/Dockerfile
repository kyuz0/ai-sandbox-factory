FROM docker.io/library/debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends squid socat tini ca-certificates python3 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' squid)" ge 6.13 \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/proxy-entrypoint
RUN chmod 755 /usr/local/bin/proxy-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/proxy-entrypoint"]
