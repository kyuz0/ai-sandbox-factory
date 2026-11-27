FROM docker.io/library/debian:trixie-slim
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=2026-11-27
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends squid socat tini ca-certificates python3 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' squid)" ge 6.13 \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/proxy-entrypoint
RUN chmod 755 /usr/local/bin/proxy-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/proxy-entrypoint"]
