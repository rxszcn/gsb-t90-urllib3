# Probe urllib3 2.8.0: one "encode the query" semantics, two engines.
# - src/urllib3/_request_methods.py:180 -> stdlib urllib.parse.urlencode (quote_plus)
# - src/urllib3/util/url.py _encode_invalid_chars via connectionpool.py:714
# In-process socket server captures the raw request line actually sent.
# Run from repo root: PYTHONPATH=src .venv/bin/python <this file>
import socket
import threading

import urllib3
from urllib3 import HTTPConnectionPool
from urllib3.util import parse_url

captured = []


def serve(sock):
    while True:
        conn, _ = sock.accept()
        try:
            data = conn.recv(65535)
            first_line = data.split(b"\r\n", 1)[0]
            captured.append(first_line.decode("latin-1"))
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
        finally:
            conn.close()


srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(8)
port = srv.getsockname()[1]
threading.Thread(target=serve, args=(srv,), daemon=True).start()

cases = [
    ("inline path+query, space", lambda p: p.request("GET", "/x?a=b c", retries=False)),
    ("dict fields, space", lambda p: p.request("GET", "/x", fields={"a": "b c"}, retries=False)),
    ("inline query with '#'", lambda p: p.request("GET", "/x?a=b#c", retries=False)),
    ("dict fields with '#'", lambda p: p.request("GET", "/x", fields={"a": "b#c"}, retries=False)),
    ("inline query stray '%'", lambda p: p.request("GET", "/x?a=100%", retries=False)),
    ("dict fields stray '%'", lambda p: p.request("GET", "/x", fields={"a": "100%"}, retries=False)),
    ("inline query tilde", lambda p: p.request("GET", "/x?a=~b", retries=False)),
    ("dict fields tilde", lambda p: p.request("GET", "/x", fields={"a": "~b"}, retries=False)),
]

pool = HTTPConnectionPool("127.0.0.1", port, timeout=5)
for name, fn in cases:
    try:
        fn(pool)
    except Exception as e:  # response is fine; defensive anyway
        print(name, "ERR", type(e).__name__, e)
        break

print("=== raw request lines seen by server ===")
for (name, _), line in zip(cases, captured):
    print(f"{name:28s} -> {line}")

print("=== parse_url readings (same inputs, other engine) ===")
print("parse_url('http://h/x?a=b c').query =", repr(parse_url("http://h/x?a=b c").query))
print("parse_url('http://h/x?a=b#c').query =", repr(parse_url("http://h/x?a=b#c").query))
print("parse_url('http://h/x?a=b c').request_uri =", repr(parse_url("http://h/x?a=b c").request_uri))
