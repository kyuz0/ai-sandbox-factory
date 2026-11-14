"""Run in a disposable Factory image: real entrypoint/socat, synthetic services."""
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time

kind, entrypoint = sys.argv[1:3]
fixture = Path('/tmp/relay-fixture')
fixture.mkdir()
# Stand-ins only for bootstrap and unrelated services; relays run real socat.
for name, script in {
    'python3': '[ "$1" = /usr/local/lib/sandbox-bootstrap.py ] && exit 0\nexec /usr/bin/python3 "$@"',
    'squid': 'exec sleep infinity', 'dbus-daemon': 'exec sleep infinity',
    'node': 'exec sleep infinity', 'gnome-keyring-daemon': 'cat >/dev/null',
}.items():
    target=fixture/name;target.write_text('#!/bin/sh\n'+script+'\n');target.chmod(0o755)
for directory in ['/run/inference','/run/egress','/run/advisor-bus','/tmp/relay-home']:
    Path(directory).mkdir(exist_ok=True)
selector=selectors.DefaultSelector()
listeners=[]
if kind=='proxy':
    addresses=[('tcp',3128)]; targets=[('unix','/run/egress/proxy.sock')]
else:
    addresses=[('unix','/run/egress/proxy.sock')]
    targets=[('tcp',3128)]
    if kind=='dev':
        addresses.append(('unix','/run/inference/proxy.sock'));targets.append(('tcp',19431))
for protocol,address in addresses:
    sock=socket.socket(socket.AF_UNIX if protocol=='unix' else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sock.bind(address if protocol=='unix' else ('127.0.0.1',address));sock.listen(1024);sock.setblocking(False)
    listeners.append(sock);selector.register(sock,selectors.EVENT_READ,True)
def serve():
    while True:
        for key,_ in selector.select():
            if key.data:
                client,_=key.fileobj.accept();selector.register(client,selectors.EVENT_READ,False)
            else:
                data=key.fileobj.recv(100)
                if data:key.fileobj.sendall(data)
                else:selector.unregister(key.fileobj);key.fileobj.close()
threading.Thread(target=serve,daemon=True).start()
env={**os.environ,'PATH':str(fixture)+':'+os.environ['PATH'],'HOME':'/tmp/relay-home',
     'CODEX_HOME':'/tmp/relay-home/.codex','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/advisor-bus/bus'}
process=subprocess.Popen(['bash',entrypoint],env=env,start_new_session=True)
clients=[]
def connect(target):
    protocol,address=target
    client=socket.socket(socket.AF_UNIX if protocol=='unix' else socket.AF_INET);client.settimeout(2)
    try:
        client.connect(address if protocol=='unix' else ('127.0.0.1',address))
        client.sendall(b'ping');assert client.recv(4)==b'ping';return client
    except BaseException:client.close();raise

def ready(target):
    deadline=time.monotonic()+30
    while True:
        assert process.poll() is None,'Entrypoint exited after relay failure'
        try:return connect(target)
        except (OSError,AssertionError):
            if time.monotonic()>deadline:raise
            time.sleep(.1)
def listener_pids():
    processes = {}
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
            if args[0].endswith(b'/socat') or args[0] == b'socat':
                parent = int((path.parent/'stat').read_text().split(')')[1].split()[1])
                processes[int(path.parent.name)] = parent
        except (OSError, IndexError):
            pass
    listeners = set()
    for pid, parent in processes.items():
        try:
            command = Path(f'/proc/{parent}/cmdline').read_bytes().split(b'\0')
            if len(command) > 1 and command[1] == b'-' and b'python' in command[0]:
                listeners.add(pid)
        except OSError:
            pass
    return listeners

try:
    for target in targets:ready(target).close()
    if '--exhaust' in sys.argv:
        before = listener_pids()
        stop=threading.Event();workers=[]
        try:
            for _ in range(600):
                worker=threading.Thread(target=stop.wait);worker.start();workers.append(worker)
        except RuntimeError:pass
        assert len(workers)<600,'Fixture did not exhaust PID limit'
        try:connect(targets[-1]).close()
        except (OSError,AssertionError):pass
        try:
            time.sleep(30)
            assert process.poll() is None, 'Entrypoint died during sustained PID exhaustion'
            assert before - listener_pids(), 'PID exhaustion did not kill a listener'
        finally:
            stop.set()
            for worker in workers:worker.join()
        ready(targets[-1]).close()
        assert listener_pids() != before, 'Listener was not restarted'
        print(f'PASS {kind}: recovered after PID exhaustion with {len(workers)} threads',flush=True)
    else:
        for _ in range(600):clients.append(connect(targets[0]))
        for client in clients:client.sendall(b'ping');assert client.recv(4)==b'ping'
        print(f'PASS {kind}: 600 simultaneous relay connections',flush=True)
    for wave in range(10):
        victims = listener_pids()
        assert victims,'Missing socat listeners'
        for pid in victims:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
        for target in targets:ready(target).close()
        for client in clients:
            client.sendall(b'ping');assert client.recv(4)==b'ping'
    print(f'PASS {kind}: ten relay failures recovered without stopping entrypoint',flush=True)
finally:
    for client in clients:client.close()
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait();raise
