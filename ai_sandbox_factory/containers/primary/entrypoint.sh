#!/bin/bash
set -euo pipefail
umask 077

# All listeners are in the controller's private network namespace. The workspace
# sees only sockets created from its own side of the connection.
socat TCP-LISTEN:3128,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/primary-egress/proxy.sock &
case "$FACTORY_PRIMARY_AGENT" in
    codex)
        socat TCP-LISTEN:3129,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/workspace-egress/proxy.sock &
        /opt/codex-proxy/bin/python -u /opt/primary/codex_auth_proxy.py &
        rm -f /run/primary-editor/auth.sock
        socat UNIX-LISTEN:/run/primary-editor/auth.sock,fork,mode=0600 TCP:127.0.0.1:3130 &
        ;;
    agy)
        mkdir -p "$HOME/.local/share/keyrings"
        rm -f /run/primary-bus/bus
        dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --nofork --nopidfile &
        printf '\n' | gnome-keyring-daemon --unlock --components=secrets --control-directory=/run/primary-bus/keyring
        if [[ -S /run/workspace-egress/proxy.sock ]]; then
            socat TCP-LISTEN:3129,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/workspace-egress/proxy.sock &
        fi
        /opt/agy-proxy/bin/python -u /opt/primary/agy_auth_proxy.py &
        rm -f /run/primary-editor/auth.sock
        socat UNIX-LISTEN:/run/primary-editor/auth.sock,fork,mode=0600 TCP:127.0.0.1:3130 &
        ;;
    claude)
        if [[ -S /run/workspace-egress/proxy.sock ]]; then
            socat TCP-LISTEN:3129,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/workspace-egress/proxy.sock &
        fi
        /opt/claude-proxy/bin/python -u /opt/primary/claude_auth_proxy.py &
        rm -f /run/primary-editor/auth.sock
        socat UNIX-LISTEN:/run/primary-editor/auth.sock,fork,mode=0600 TCP:127.0.0.1:3130 &
        ;;
esac
touch /tmp/factory-primary-ready
wait -n
