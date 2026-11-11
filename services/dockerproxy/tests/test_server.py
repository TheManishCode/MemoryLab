"""The proxy against a fake daemon on a unix socket: what is forwarded (rebuilt, never the client's raw bytes), what never reaches the
daemon, and that a hijacked attach works in both directions."""

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from dockerproxy.server import Proxy
from payloads import GOOD, NAME, POLICY, mutated


class FakeDaemon:
    """Records every request it receives; answers inspect from `containers`, 201 to a create, 204 otherwise, and echoes after an upgrade."""

    def __init__(self, containers: dict[str, dict[str, Any]]) -> None:
        self.containers, self.requests, self.streams = containers, [], []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await reader.read(4096)
            if not chunk:
                return
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v for k, _, v in (x.partition(": ") for x in lines[1:])}
        n = int(headers.get("content-length", "0"))
        while len(rest) < n:
            rest += await reader.read(4096)
        self.requests.append({"line": lines[0], "headers": headers, "body": rest[:n], "after": rest[n:]})
        method, target, _ = lines[0].split(" ")
        parts = target.split("/")
        if method == "GET" and target.endswith("/json") and "/containers/" in target:
            ident = parts[-2]
            doc = self.containers.get(ident)
            if doc is None:
                writer.write(b"HTTP/1.0 404 Not Found\r\n\r\n{}")
            else:
                writer.write(b"HTTP/1.0 200 OK\r\n\r\n" + json.dumps(doc).encode())
        elif headers.get("upgrade") == "tcp":
            writer.write(b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            await writer.drain()
            got = rest[n:]
            while True:
                if got:
                    writer.write(b"echo:" + got)
                    await writer.drain()
                got = await reader.read(4096)
                if not got:
                    break
            self.streams.append("closed")
        elif method == "POST" and target.split("?")[0].endswith("/containers/create"):
            body = b'{"Id":"abc123","Warnings":[]}'
            writer.write(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nConnection: keep-alive\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        else:
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\nApi-Version: 1.56\r\n\r\n")
        await writer.drain()
        writer.close()


ours = {"Name": f"/{NAME}", "Config": {"Labels": {"memorylab.owner": "memorylab"}}}
theirs = {"Name": "/postgres", "Config": {"Labels": {}}}


async def _start_daemon(daemon: FakeDaemon) -> tuple[asyncio.AbstractServer, str, tempfile.TemporaryDirectory[str] | None]:
    """Serve the fake daemon the way the proxy will reach it: a unix socket where the platform has
    them (Linux), otherwise TCP (Windows). Returns the server, the endpoint the proxy connects to,
    and a temp dir to clean up (unix only)."""
    if hasattr(asyncio, "start_unix_server"):
        tmp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "docker.sock")
        return await asyncio.start_unix_server(daemon.handle, path), path, tmp
    up = await asyncio.start_server(daemon.handle, "127.0.0.1", 0)
    return up, f"tcp:127.0.0.1:{up.sockets[0].getsockname()[1]}", None


def with_proxy(test: Callable[[FakeDaemon, int], Awaitable[None]], containers: dict[str, dict[str, Any]] | None = None) -> None:
    async def main() -> None:
        daemon = FakeDaemon(containers if containers is not None else {NAME: ours, "abc123": ours, "postgres": theirs})
        up, target, tmp = await _start_daemon(daemon)
        try:
            proxy = Proxy(POLICY, target, head_timeout_s=2)
            server = await proxy.start("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                await test(daemon, port)
            finally:
                server.close()
                up.close()
        finally:
            if tmp is not None:
                tmp.cleanup()

    asyncio.run(main())


async def call(port: int, raw: bytes, *, read_all: bool = True) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5) if read_all else b""
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines and lines[0] else 0
    return status, {k.lower(): v for k, _, v in (x.partition(": ") for x in lines[1:])}, body


def req(method: str, target: str, body: bytes = b"", **headers: str) -> bytes:
    h = {"Host": "127.0.0.1:2375", "User-Agent": "Docker-Client/29.9.2 (linux)", **headers}
    if body or method == "POST":
        h["Content-Length"] = str(len(body))
    return (f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body


def test_an_allowed_call_is_rebuilt_and_forwarded_with_connection_close() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        status, headers, _ = await call(port, req("POST", f"/v1.56/containers/{NAME}/kill", Authorization="Bearer x", **{"X-Forwarded-For": "1.2.3.4", "Accept-Encoding": "gzip"}))
        assert status == 204 and headers["connection"] == "close" and headers["api-version"] == "1.56"
        sent = daemon.requests[-1]
        assert sent["line"] == f"POST /v1.56/containers/{NAME}/kill HTTP/1.1"
        assert set(sent["headers"]) <= {"host", "user-agent", "content-length", "connection"}          # nothing else of the client's is passed on
        assert sent["headers"]["connection"] == "close"

    with_proxy(t)


def test_a_sandbox_create_goes_through_and_a_bad_one_never_reaches_the_daemon() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        target = f"/v1.56/containers/create?name={NAME}"
        status, _, body = await call(port, req("POST", target, json.dumps(GOOD).encode(), **{"Content-Type": "application/json"}))
        assert status == 201 and json.loads(body)["Id"] == "abc123"
        sent = len(daemon.requests)
        for doc in (mutated(**{"HostConfig.Privileged": True}), mutated(**{"HostConfig.Binds": ["/:/host"]}), mutated(**{"HostConfig.NetworkMode": "host"}),
                    mutated(Image="alpine")):
            status, _, body = await call(port, req("POST", target, json.dumps(doc).encode(), **{"Content-Type": "application/json"}))
            assert status == 403 and "not allowed" in json.loads(body)["message"]
        assert len(daemon.requests) == sent                                   # none of the four reached the daemon

    with_proxy(t)


def test_a_disallowed_endpoint_never_reaches_the_daemon() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        for method, target in (("GET", "/containers/json"), ("POST", "/images/create?fromImage=alpine"), ("POST", f"/containers/{NAME}/exec"),
                               ("GET", "/info"), ("DELETE", f"/containers/{NAME}%2f..")):
            status, _, body = await call(port, req(method, target))
            assert status == 403 and json.loads(body)["message"].startswith("memorylab docker proxy"), (method, target)
        assert daemon.requests == []

    with_proxy(t)


def test_a_call_on_a_container_that_is_not_ours_is_refused_and_a_missing_one_is_a_404() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        for method, target in (("POST", "/v1.56/containers/postgres/kill"), ("DELETE", "/v1.56/containers/postgres?force=1"), ("POST", "/v1.56/containers/postgres/start")):
            status, _, _ = await call(port, req(method, target))
            assert status == 403
        status, _, body = await call(port, req("DELETE", "/v1.56/containers/nothing-here"))
        assert status == 404 and "No such container" in json.loads(body)["message"]
        assert [r["line"].split(" ")[0] for r in daemon.requests] == ["GET"] * 4          # only inspects: no kill, no remove, no start was forwarded
        status, _, _ = await call(port, req("DELETE", f"/v1.56/containers/{NAME}?force=1"))      # ours: forwarded
        assert status == 204 and daemon.requests[-1]["line"].startswith("DELETE")

    with_proxy(t)


def test_chunked_duplicate_and_oversized_requests_are_refused() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        target = f"/v1.56/containers/create?name={NAME}"
        for raw in (
            b"POST " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"POST " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 500\r\n\r\n12345",
            b"POST " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n",
            b"POST " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n",
            b"POST " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",                       # a create needs a body
            b"GET /_ping HTTP/1.1\r\nHost: x\r\n folded: header\r\n\r\n",
            b"GET /_ping HTTP/1.0\r\n\r\n",
            b"GET /_ping\r\n\r\n",
            b"\x00\x01garbage\r\n\r\n",
            b"GET /_ping HTTP/1.1\r\nHost: x\r\n" + b"X-Pad: " + b"a" * 20000 + b"\r\n\r\n",
        ):
            status, _, _ = await call(port, raw)
            assert status in (400, 413), raw[:60]
        assert daemon.requests == []

    with_proxy(t)


def test_a_second_request_smuggled_behind_the_first_is_not_forwarded() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        raw = req("HEAD", "/_ping") + req("POST", "/v1.56/containers/postgres/kill")
        await call(port, raw)
        await asyncio.sleep(0.2)
        assert [r["line"] for r in daemon.requests] == ["HEAD /_ping HTTP/1.1"]
        # and a body that contains a whole request is just a body
        body = json.dumps(GOOD).encode() + b"\r\n\r\nPOST /containers/postgres/kill HTTP/1.1\r\n\r\n"
        await call(port, req("POST", f"/v1.56/containers/create?name={NAME}", body[: len(json.dumps(GOOD))]) + body[len(json.dumps(GOOD)):])
        assert all("postgres" not in r["line"] for r in daemon.requests)

    with_proxy(t)


def test_attach_is_a_two_way_stream_and_bytes_sent_with_the_request_are_kept() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(req("POST", "/v1.56/containers/abc123/attach?stderr=1&stdin=1&stdout=1&stream=1", Connection="Upgrade", Upgrade="tcp") + b"first-line\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 101")
        assert await asyncio.wait_for(reader.readuntil(b"\n"), 5) == b"echo:first-line\n"          # stdin written with the request arrived
        writer.write(b"second\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.readuntil(b"\n"), 5) == b"echo:second\n"
        writer.write_eof()                                                                         # stdin closed: the daemon side sees EOF
        await asyncio.sleep(0.3)
        assert daemon.streams == ["closed"]
        writer.close()

    with_proxy(t)


def test_attach_to_a_container_that_is_not_ours_is_refused_before_any_upgrade() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        status, _, _ = await call(port, req("POST", "/v1.56/containers/postgres/attach?stream=1&stdin=1", Connection="Upgrade", Upgrade="tcp"))
        assert status == 403
        assert all("attach" not in r["line"] for r in daemon.requests)

    with_proxy(t)


def test_the_daemon_receives_the_canonical_rebuild_not_the_clients_bytes() -> None:
    from dockerproxy.policy import parse_create

    async def t(daemon: FakeDaemon, port: int) -> None:
        canonical = parse_create(json.dumps(GOOD).encode(), NAME, POLICY)[1]
        messy = json.dumps(dict(reversed(list(GOOD.items()))), indent=4).encode()
        status, _, _ = await call(port, req("POST", f"/v1.56/containers/create?name={NAME}", messy, **{"Content-Type": "application/json"}))
        assert status == 201
        sent = daemon.requests[-1]
        assert sent["body"] == canonical and sent["headers"]["content-length"] == str(len(canonical))

    with_proxy(t)


def test_the_reviewers_second_spelling_payloads_never_reach_the_daemon() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        for extra in ({"image": "memorylab-egress:dev"}, {"user": "0"}, {"cmd": ["sh"]}, {"hostconfig": {"Binds": ["/:/h"]}},
                      {"hostConfig": {"Privileged": True, "Binds": ["/:/host"]}}):
            body = json.dumps({**GOOD, **extra}).encode()
            status, _, _ = await call(port, req("POST", f"/v1.56/containers/create?name={NAME}", body, **{"Content-Type": "application/json"}))
            assert status == 403
        assert daemon.requests == []

    with_proxy(t)


def test_a_body_on_any_call_but_create_is_refused() -> None:
    async def t(daemon: FakeDaemon, port: int) -> None:
        for method, target in (("POST", f"/v1.56/containers/{NAME}/start"), ("POST", f"/v1.56/containers/{NAME}/kill"),
                               ("POST", f"/v1.56/containers/{NAME}/wait"), ("DELETE", f"/v1.56/containers/{NAME}"),
                               ("GET", f"/v1.56/containers/{NAME}/json"), ("POST", "/v1.56/containers/abc123/attach?stream=1&stdin=1")):
            status, _, _ = await call(port, req(method, target, b'{"x":1}', **{"Content-Type": "application/json"}))
            assert status == 403, (method, target)
        assert daemon.requests == []

    with_proxy(t)
