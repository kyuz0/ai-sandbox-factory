# Test a native VS Code extension with an existing CLI login

Run this on a Linux host with rootless Podman, Chromium, Node.js, Python Playwright and an existing CLI login for the agent being tested. Each run uses one short live subscription request. Use the official unpacked Linux extension and the extracted code-server distribution. The test prints the result and deletes its disposable containers, private homes and named volumes when it exits.

From the Factory checkout, use the Python environment in which Factory and Playwright are installed. The proxy and selected agent images must already exist locally. Creating a disposable sandbox with that agent through Factory prepares them.

```sh
PYTHONPATH=. python research/primary-agents/native-editor/check.py claude \
  --extension /path/to/anthropic.claude-code-linux-x64 \
  --code-server /path/to/code-server-linux-amd64 \
  --chromium /usr/bin/chromium-browser
```

Choose `codex`, `claude` or `agy` and supply the matching extension directory containing `package.json`. The test copies the sign-in into a private home used only by the disposable editor. It opens the real panel, asks the agent to join two words, and checks for the joined pair in the reply. It starts with an empty workspace. Only the controller can reach the provider through the configured proxy.

For Codex and Claude, add `--separate` to test a different arrangement. The extension runs in an offline workspace without the account files or provider proxy. The signed-in CLI runs in a second container. The test gives the workspace a random value, asks the panel to copy a file, checks the reply and verifies the output file in the workspace. It also verifies the output file is absent from the controller. The bridge accepts this fixed task only. It is a test fixture.

For Antigravity, the test starts its native hub with a private keyring. Chromium reaches the hub through a second authenticated loopback port. The editor and hub share the disposable account in this test.
