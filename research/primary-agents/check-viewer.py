"""Verify a signed host viewer URL while the CLI's fixed browser tool is active."""
import base64
import http.client
import ipaddress
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

container, client = sys.argv[1:]
def inside(*args):
    return subprocess.run(['podman','exec',container,*args],capture_output=True,text=True)
for _ in range(80):
    link = inside('cat', '/tmp/view-' + client)
    if link.returncode == 0 and link.stdout.strip():
        break
    time.sleep(.25)
else:
    sys.exit('No browser session published by ' + client)
url = urlsplit(link.stdout.strip())
address = '127.0.0.1' if url.hostname.endswith('.localhost') else url.hostname
assert ipaddress.ip_address(address).is_loopback
http = http.client.HTTPConnection(address, url.port, timeout=8)
http.request('GET',url.path,headers={'Host':url.netloc})
page=http.getresponse()
assert page.status == 200 and b'Factory fixture' in page.read()
with socket.create_connection((address,url.port),timeout=8) as sock:
    key=base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f'GET {url.path}vnc HTTP/1.1\r\nHost: {url.netloc}\r\nOrigin: http://{url.netloc}\r\n'
                  f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n').encode())
    buf=b''
    while b'\r\n\r\n' not in buf:
        packet = sock.recv(2048)
        assert packet
        buf += packet
    header, data = buf.split(b'\r\n\r\n',1)
    assert b'101 ' in header
    while b'RFB ' not in data:
        packet=sock.recv(256)
        assert packet
        data+=packet
assert inside('touch','/tmp/checked-'+client).returncode == 0
print(client, 'host viewer showed the fixture tab and delivered a VNC handshake')
