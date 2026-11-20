"""Check the default sandbox's command-line tools without using the network."""
from pathlib import Path
import shutil
import subprocess
import tempfile


commands = """
    python3 node npm git ssh scp sftp curl wget ping ip ss dig getent nc traceroute
    find fd rg grep sed awk sort uniq xargs diff patch tree column file hexdump xxd jq
    rsync tar gzip bzip2 xz zip unzip zstd cpio gcc g++ make pkg-config
    less nano vi vim tmux man apropos ps top lsof fuser pstree killall flock
    getfacl setfacl bc envsubst netstat nslookup /usr/bin/time
""".split()
missing = [command for command in commands if shutil.which(command) is None]
if missing:
    raise SystemExit('Missing CLI tools: ' + ', '.join(missing))

# Verify the pages as well as the reader. Debian slim normally discards them.
for page in ('ls', 'bash', 'find', 'git', 'fd', 'make'):
    subprocess.run(['man', '-w', page], check=True, stdout=subprocess.DEVNULL)

with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    source = work / 'hello.cpp'
    source.write_text('int main() { return 1; }\n')
    subprocess.run(['patch', 'hello.cpp'], cwd=work, check=True, text=True,
                   input='@@ -1 +1 @@\n-int main() { return 1; }\n+int main() { return 0; }\n',
                   stdout=subprocess.DEVNULL)
    subprocess.run(['make', 'hello'], cwd=work, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([work / 'hello'], cwd=work, check=True)
    archive = work / 'source.tar.zst'
    subprocess.run(['tar', '--zstd', '-cf', archive, 'hello.cpp'], cwd=work, check=True)
    restored = subprocess.check_output(['tar', '--zstd', '-xOf', archive, 'hello.cpp'], cwd=work)
    if restored != source.read_bytes():
        raise SystemExit('Archive round trip failed')

print('PASS base CLI tools, offline manuals, patch, C++ build and zstd archive')
