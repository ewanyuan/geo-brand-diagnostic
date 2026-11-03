"""Application-only CONNECT relay. It never decrypts or logs browser traffic.

Selected public model hosts use the existing Wi-Fi interface. Public DNS lookup
is scoped to this relay; Windows DNS, routing, VPNs and other apps are untouched.
Other hosts keep using the user's existing loopback proxy.
"""

import ipaddress
import base64
import http.client
import json
import os
import secrets
import select
import socket
import ssl
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener
from browser_runtime import configuration

DOMAINS = ("deepseek.com", "doubao.com", "kimi.com", "moonshot.cn", "qianwen.com",
           "baidu.com", "bdstatic.com", "bcebos.com", "tencent.com", "qq.com", "gtimg.com")
_server = None
_cache = {}
_lock = threading.Lock()


def direct_host(host):
    configured = configuration().get("direct_domains", ("deepseek.com", "doubao.com"))
    roots = [root for root in configured if root in DOMAINS] if isinstance(configured, (list, tuple)) else []
    return any(host == root or host.endswith("." + root) for root in roots)


def _skip_name(data, pos):
    for _ in range(128):
        size = data[pos]
        if size & 192 == 192:
            return pos + 2
        pos += 1
        if size == 0:
            return pos
        pos += size
    raise OSError("Invalid DNS name")


def physical_addresses(host, interface):
    query_id = secrets.randbelow(65536)
    query = struct.pack("!HHHHHH", query_id, 256, 1, 0, 0, 0)
    query += b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".")) + b"\0\0\1\0\1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(interface))
        sock.connect(("223.5.5.5", 443))
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname="dns.alidns.com")
        path = "/dns-query?dns=" + base64.urlsafe_b64encode(query).decode().rstrip("=")
        tls.sendall((f"GET {path} HTTP/1.1\r\nHost: dns.alidns.com\r\nAccept: application/dns-message\r\nConnection: close\r\n\r\n").encode("ascii"))
        response = http.client.HTTPResponse(tls)
        response.begin()
        data = response.read(65536)
        if response.status != 200:
            raise OSError("DNS response unavailable")
        ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", data[:12])
        if ident != query_id or flags & 15:
            raise OSError("Invalid DNS response")
        pos = 12
        for _ in range(questions):
            pos = _skip_name(data, pos) + 4
        result = []
        for _ in range(answers):
            pos = _skip_name(data, pos)
            typ, _, _, size = struct.unpack("!HHIH", data[pos:pos + 10])
            pos += 10
            if typ == 1 and size == 4:
                address = socket.inet_ntoa(data[pos:pos + size])
                if ipaddress.ip_address(address).is_global:
                    result.append(address)
            pos += size
        tls.close()
        if not result:
            raise OSError("No public DNS answer")
        return result
    finally:
        sock.close()


def public_addresses(host, upstream, interface):
    with _lock:
        cached = _cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    try:
        addresses = physical_addresses(host, interface)
    except (OSError, ValueError, IndexError, struct.error):
        opener = build_opener(ProxyHandler({"http": upstream, "https": upstream}))
        with opener.open("https://dns.alidns.com/resolve?" + urlencode({"name": host, "type": "A"}), timeout=10) as response:
            data = json.load(response)
        addresses = [row["data"] for row in data.get("Answer", []) if row.get("type") == 1
                     and ipaddress.ip_address(row["data"]).is_global]
    if not addresses:
        raise OSError("No public address")
    with _lock:
        _cache[host] = (time.monotonic() + 60, addresses)
    return addresses


def connect(host, port, upstream, interface):
    if port != 443:
        raise OSError("Only HTTPS is supported")
    if direct_host(host):
        for address in public_addresses(host, upstream, interface):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(7)
            try:
                if os.name == "nt":
                    sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(interface))
                sock.connect((address, port))
                return sock
            except OSError:
                sock.close()
        raise OSError("Direct connection unavailable")
    parsed = urlsplit(upstream)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise OSError("Loopback upstream required")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
    header = b""
    while not header.endswith(b"\r\n\r\n") and len(header) < 8192:
        part = sock.recv(1)
        if not part:
            sock.close(); raise OSError("Upstream closed")
        header += part
    if b" 200 " not in header.split(b"\r\n", 1)[0]:
        sock.close(); raise OSError("Upstream refused")
    return sock


def start(upstream, interface):
    global _server
    if _server:
        return "http://127.0.0.1:" + str(_server.server_port)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_CONNECT(self):
            remote = None
            try:
                host, port = self.path.rsplit(":", 1)
                if not host or len(host) > 253 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
                    raise OSError("Invalid host")
                remote = connect(host.lower(), int(port), upstream, interface)
                self.send_response(200, "Connection established"); self.end_headers()
                self.connection.settimeout(None); remote.settimeout(None)
                while True:
                    readable, _, _ = select.select([self.connection, remote], [], [], 90)
                    if not readable: break
                    for source in readable:
                        content = source.recv(65536)
                        if not content: return
                        (remote if source is self.connection else self.connection).sendall(content)
            except (OSError, ValueError):
                if not remote:
                    self.send_error(502)
            finally:
                self.close_connection = True
                if remote: remote.close()
    _server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:" + str(_server.server_port)
