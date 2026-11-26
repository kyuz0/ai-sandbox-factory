# The native client needs its sign-in transport. Project shells use the
# workspace network instead, without changing the parent client's environment.
factory_workspace_proxy() {
    if [ "$2" = 'http://127.0.0.1:3130' ]; then
        if [ "${SANDBOX_NETWORK_MODE-}" = 'allowlist' ]; then
            export "$1=http://127.0.0.1:3128"
        else
            unset "$1"
        fi
    fi
}
factory_workspace_proxy HTTP_PROXY "${HTTP_PROXY-}"
factory_workspace_proxy HTTPS_PROXY "${HTTPS_PROXY-}"
factory_workspace_proxy http_proxy "${http_proxy-}"
factory_workspace_proxy https_proxy "${https_proxy-}"
unset -f factory_workspace_proxy
