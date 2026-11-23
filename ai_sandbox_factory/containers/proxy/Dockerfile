FROM docker.io/library/debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends squid socat tini ca-certificates python3 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' squid)" ge 6.13 \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/proxy-entrypoint
RUN chmod 755 /usr/local/bin/proxy-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/proxy-entrypoint"]
