#!/bin/bash
set -euo pipefail
umask 077

# All listeners are in the controller's private network namespace. The workspace
# sees only sockets created from its own side of the connection.
socat TCP-LISTEN:3128,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/primary-egress/proxy.sock &
case "$FACTORY_PRIMARY_AGENT" in
    codex)
        socat TCP-LISTEN:36781,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/primary-workspace/exec.sock &
        ;;
    agy)
        mkdir -p "$HOME/.local/share/keyrings"
        rm -f /run/primary-bus/bus
        dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --nofork --nopidfile &
        printf '\n' | gnome-keyring-daemon --unlock --components=secrets --control-directory=/run/primary-bus/keyring
        ;;
    claude) ;;
esac
touch /tmp/factory-primary-ready
wait -n
