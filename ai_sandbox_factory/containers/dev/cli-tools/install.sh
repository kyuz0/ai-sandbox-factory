#!/bin/sh
set -eu
# Debian slim removes manuals; retain new pages and restore the base utilities' pages.
echo 'path-include=/usr/share/man/*' > /etc/dpkg/dpkg.cfg.d/zz-cli-manuals
apt-get update
apt-get install -y --no-install-recommends --reinstall \
    bash coreutils diffutils findutils grep gzip mawk sed tar util-linux
apt-get install -y --no-install-recommends \
    bash-completion man-db manpages manpages-dev git-man \
    build-essential pkg-config patch tree bsdextrautils xxd acl bc time gettext-base \
    zstd cpio lsof psmisc net-tools traceroute \
    ca-certificates curl wget git python3 socat tini procps ripgrep iproute2 iputils-ping \
    dnsutils netcat-openbsd jq less nano vim file zip unzip bzip2 xz-utils rsync tmux \
    openssh-client fd-find
ln -sf /usr/bin/fdfind /usr/local/bin/fd
install -d /usr/local/share/man/man1
ln -sf /usr/share/man/man1/fdfind.1.gz /usr/local/share/man/man1/fd.1.gz
# ping must start with all container capabilities dropped; do not grant privileges.
python3 -c 'import os; p="/usr/bin/ping"; os.removexattr(p, "security.capability") if "security.capability" in os.listxattr(p) else None'
chmod u-s /usr/bin/ping
rm -rf /var/lib/apt/lists/*
