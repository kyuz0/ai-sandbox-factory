"""Exercise native account clients against an already-created offline marker workspace."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("client", choices=["codex", "claude", "agy"])
parser.add_argument("directory", type=Path)
parser.add_argument("--edit", action="store_true", help="Test Codex native workspace editing")
args = parser.parse_args()
root = args.directory
cid = (root / "cid").read_text().strip()
work = root / "work"
socket = root / (args.client + ".sock")
tool = ["/usr/bin/socat", "STDIO", "UNIX-CONNECT:" + str(socket)]
env = os.environ.copy()
for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
             "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH"):
    env.pop(name, None)
prompt = "Use the registered MCP server named workspace and its tool named read_marker, with empty arguments {}. If tools are lazy-loaded, use call_mcp_tool with ServerName workspace, ToolName read_marker and Arguments {}. Reply only with its exact returned value. Use no other tools."
private_route = "/" + secrets.token_urlsafe(24)


async def forward(reader, writer):
    # Only the random loopback URL issued to this controller can reach the offline exec-server.
    proc = None
    async def copy(src, dst):
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        line, rest = head.split(b"\r\n", 1)
        if line != f"GET {private_route} HTTP/1.1".encode():
            return
        proc = await asyncio.create_subprocess_exec(
            "podman", "exec", "-i", cid, "socat", "STDIO", "TCP:127.0.0.1:36781",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        proc.stdin.write(b"GET / HTTP/1.1\r\n" + rest)
        tasks = [asyncio.create_task(copy(reader, proc.stdin)), asyncio.create_task(copy(proc.stdout, writer))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if proc:
            proc.stdin.close()
            await proc.wait()
        writer.close()


async def main():
    async def marker_channel(reader, writer):
        proc = await asyncio.create_subprocess_exec(
            "podman", "exec", "-i", cid, "python3", "/proof/marker-tool.py",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        async def pipe(source, dest):
            try:
                while chunk := await source.read(8192):
                    dest.write(chunk)
                    await dest.drain()
            finally:
                dest.close()
        await asyncio.gather(pipe(reader, proc.stdin), pipe(proc.stdout, writer), return_exceptions=True)
        await proc.wait()
    socket.unlink(missing_ok=True)
    channel = await asyncio.start_unix_server(marker_channel, str(socket))
    server = None
    if args.client == "codex":
        server = await asyncio.start_server(forward, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        env["CODEX_EXEC_SERVER_URL"] = f"ws://127.0.0.1:{port}{private_route}"
        cmd = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
               "-s", "workspace-write" if args.edit else "read-only", "-c", 'model_reasoning_effort="low"', "--json",
               "Run cat /workspace/marker with the native shell tool. Reply only with the exact value. Use no other tools."]
        if args.edit:
            cmd[-1] = "Use your shell to read /workspace/marker, then use apply_patch to create proof.txt in the current working directory containing that exact value. Reply only with the value."
    elif args.client == "claude":
        env["ENABLE_TOOL_SEARCH"] = "false"
        mcp = {"mcpServers": {"factory_probe": {"command": tool[0], "args": tool[1:]}}}
        cmd = ["claude", "-p", "--restricted", "--tools", "", "--strict-mcp-config",
               "--mcp-config", json.dumps(mcp), "--allowedTools", "mcp__factory_probe__read_marker",
               "--no-session-persistence", "--output-format", "json", "--", prompt.replace("workspace and", "factory_probe and").replace("ServerName workspace", "ServerName factory_probe")]
    else:
        env["HOME"] = str(root / "agy-home")
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            env.pop(key, None)
        config = Path(env["HOME"]) / ".gemini" / "config"
        config.mkdir(parents=True, exist_ok=True)
        hook = Path(__file__).resolve().with_name("agy-tool-policy.py")
        command = shlex.join([sys.executable, str(hook)])
        policy = {"factory_probe": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": command}]}]}}
        (config / "hooks.json").write_text(json.dumps(policy))
        subprocess.run(["agy", "mcp", "add", "workspace", *tool], check=True,
                       env=env, cwd=work, stdout=subprocess.DEVNULL)
        settings = {"modelProvider": "", "permissions": {
            "deny": ["command(*)", "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)"],
            "allow": ["mcp(workspace/read_marker)"]}}
        cmd = ["agy", "--settings", json.dumps(settings), "--output-format", "text", "--print=First try the native run_command tool to execute pwd and expect it to be denied. Then " + prompt]
    try:
        with (root / (args.client + ".log")).open("wb") as log:
            client = await asyncio.create_subprocess_exec(*cmd, env=env, cwd=work, stdout=log, stderr=log)
            rc = await client.wait()
    finally:
        channel.close()
        await channel.wait_closed()
        socket.unlink(missing_ok=True)
        if server:
            server.close()
            await server.wait_closed()
    marker = subprocess.check_output(["podman", "exec", cid, "cat", "/workspace/marker"], text=True)
    log = (root / (args.client + ".log")).read_text(errors="replace")
    if args.client == "codex":
        events = [json.loads(line) for line in log.splitlines() if line.startswith("{")]
        passed = any(e.get("item", {}).get("type") == "agent_message" and e["item"]["text"].strip() == marker for e in events)
    elif args.client == "claude":
        try:
            passed = json.loads(log)["result"].strip() == marker
        except (KeyError, ValueError):
            passed = False
    else:
        decisions = (root / "agy-home" / "proof-tool-decisions").read_text().splitlines()
        passed = log.strip() == marker and "run_command deny" in decisions and "call_mcp_tool allow" in decisions
    if args.edit:
        written = subprocess.run(["podman", "exec", cid, "cat", str(work / "proof.txt")], capture_output=True, text=True)
        edited = written.returncode == 0 and written.stdout.strip() == marker and not (work / "proof.txt").exists()
        print("Native patch landed solely in container:", edited)
        passed = passed and edited
    print(args.client, "exit", rc, "returned worker-only value", passed)
    return 0 if passed and not rc else 1


sys.exit(asyncio.run(main()))
