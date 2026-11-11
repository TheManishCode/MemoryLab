"""The filtering HTTP proxy in front of the Docker socket.

One request per client connection (`Connection: close` both ways), so there is no pipelining and no request smuggling across requests: the
request line and headers are parsed here and a NEW request is built for the daemon; the client's bytes are never forwarded as they came,
except the hijacked stream after an allowed `attach` (101), which is the whole point of attach. Chunked requests are refused."""

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

from .policy import MAX_BODY, Policy, Verdict, is_ours, parse_create, route

log = logging.getLogger("dockerproxy")
MAX_HEAD = 16 * 1024
REQUEST_LINE = re.compile(r"([A-Z]{3,7}) (\S{1,2048}) HTTP/1\.1")
HEADER = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}):[ \t]*(.{0,1024}?)[ \t]*")
REASONS = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 502: "Bad Gateway"}


def parse_daemon(spec: str) -> tuple[str, int] | None:
    """Resolve the daemon endpoint: `(host, port)` for TCP, or ``None`` for a unix socket at `spec`.

    A ``tcp:host:port`` / ``tcp://host:port`` spec selects TCP; any other value is a unix socket
    path (the normal Linux case, `/var/run/docker.sock`). On a platform without unix sockets
    (Windows) a non-TCP spec is rejected with guidance, since Docker there is reached over TCP."""
    low = spec.lower()
    if low.startswith("tcp:"):
        rest = spec[4:].lstrip("/")
        host, _, port = rest.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"malformed tcp daemon endpoint {spec!r}; expected tcp:host:port")
        return host, int(port)
    if not hasattr(asyncio, "open_unix_connection"):
        raise ValueError(
            f"this platform has no unix sockets; pass a tcp daemon endpoint such as "
            f"tcp:127.0.0.1:2375 (Docker Desktop: enable 'Expose daemon on tcp://'), not {spec!r}"
        )
    return None


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class Proxy:
    def __init__(self, policy: Policy, socket_path: str, *, max_connections: int = 32, head_timeout_s: float = 10.0) -> None:
        self.policy, self.socket_path, self.head_timeout_s = policy, socket_path, head_timeout_s
        self._daemon_tcp = parse_daemon(socket_path)  # (host, port) for TCP, or None for a unix socket
        self._slots = asyncio.Semaphore(max_connections)

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port, limit=MAX_HEAD * 2)

    async def _open_daemon(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to the Docker daemon: TCP when configured, otherwise its unix socket."""
        if self._daemon_tcp is not None:
            return await asyncio.open_connection(*self._daemon_tcp)
        return await asyncio.open_unix_connection(self.socket_path)

    # ---- daemon side ------------------------------------------------------------------------
    async def _inspect(self, ident: str) -> dict[str, Any] | None:
        reader, writer = await self._open_daemon()
        try:
            writer.write(f"GET /containers/{ident}/json HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
            await writer.drain()
            raw = await reader.read()
        finally:
            writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 502
        if status == 404:
            return None
        if status != 200:
            raise Refused(502, "the daemon could not inspect the container")
        doc = json.loads(body)
        return doc if isinstance(doc, dict) else None

    # ---- client side ------------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with self._slots:
                await self._serve(reader, writer)
        except Refused as exc:
            await self._reply(writer, exc.status, exc.message)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        except Exception:
            log.exception("proxy error")
            with contextlib.suppress(Exception):
                await self._reply(writer, 502, "proxy error")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _reply(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = json.dumps({"message": message}).encode()
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD:
                raise Refused(400, "request head too large")
            chunk = await asyncio.wait_for(reader.read(4096), self.head_timeout_s)
            if not chunk:
                raise ConnectionError
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _parse(self, head: bytes) -> tuple[str, str, dict[str, str]]:
        try:
            lines = head.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            raise Refused(400, "malformed request") from None
        m = REQUEST_LINE.fullmatch(lines[0])
        if not m:
            raise Refused(400, "malformed request line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            h = HEADER.fullmatch(line)
            if not h or line[0] in " \t":
                raise Refused(400, "malformed header")
            name = h.group(1).lower()
            if name in headers:
                raise Refused(400, "repeated header")
            headers[name] = h.group(2)
        if "transfer-encoding" in headers:
            raise Refused(400, "chunked requests are not accepted")
        return m.group(1), m.group(2), headers

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, rest = await self._read_head(reader)
        method, target, headers = self._parse(head)
        verdict = route(method, target, self.policy)
        if not verdict.allow:
            log.warning("refused %s %s: %s", method, target.split("?")[0][:120], verdict.reason)
            raise Refused(403, f"memorylab docker proxy: {verdict.reason}")
        length = headers.get("content-length", "0")
        if not length.isdigit() or int(length) > MAX_BODY:
            raise Refused(413 if length.isdigit() else 400, "bad content-length")
        n = int(length)
        if verdict.needs_body and n == 0 or (not verdict.needs_body and n):
            raise Refused(403 if n else 400, "memorylab docker proxy: " + ("this call takes no body" if n else "a body is required"))
        body = rest[:n]
        while len(body) < n:
            chunk = await asyncio.wait_for(reader.read(n - len(body)), self.head_timeout_s)
            if not chunk:
                raise ConnectionError
            body += chunk
        leftover = rest[n:] if verdict.upgrade else b""
        body = await self._authorise(verdict, target, body)
        await self._forward(verdict, method, target, headers, body, leftover, reader, writer)

    async def _authorise(self, verdict: Verdict, target: str, body: bytes) -> bytes:
        """Returns the body to forward: for a create, a NEW document built from the validated fields only, never the client's bytes."""
        if verdict.route == "create":
            reasons, canonical = parse_create(body, verdict.container or "", self.policy)
            if reasons:
                log.warning("refused a container create: %s", ", ".join(reasons))
                raise Refused(403, "memorylab docker proxy: this container configuration is not allowed: " + ", ".join(reasons))
            return canonical
        if verdict.container is not None:
            doc = await self._inspect(verdict.container)
            owned = is_ours(doc)
            if owned is None:
                raise Refused(404, f"No such container: {verdict.container}")
            if not owned:
                log.warning("refused %s on a container that is not ours", verdict.route)
                raise Refused(403, "memorylab docker proxy: not a memorylab sandbox")
        return body

    async def _forward(self, verdict: Verdict, method: str, target: str, headers: dict[str, str], body: bytes, leftover: bytes,
                       reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ureader, uwriter = await self._open_daemon()
        try:
            lines = [f"{method} {target} HTTP/1.1", "Host: docker", "User-Agent: " + headers.get("user-agent", "memorylab-dockerproxy")[:200]]
            if "content-type" in headers:
                lines.append("Content-Type: " + headers["content-type"][:100])
            if body or method == "POST":
                lines.append(f"Content-Length: {len(body)}")
            lines += ["Connection: Upgrade", "Upgrade: tcp"] if verdict.upgrade else ["Connection: close"]
            uwriter.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body + leftover)
            await uwriter.drain()
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = await ureader.read(4096)
                if not chunk:
                    raise Refused(502, "the daemon closed the connection")
                raw += chunk
                if len(raw) > MAX_HEAD:
                    raise Refused(502, "the daemon's response head is too large")
            rhead, _, rbody = raw.partition(b"\r\n\r\n")
            status_line, *rheaders = rhead.decode("latin-1").split("\r\n")
            status = int(status_line.split(" ", 2)[1])
            if status == 101:
                writer.write(rhead + b"\r\n\r\n" + rbody)
                await writer.drain()
                await self._splice(reader, writer, ureader, uwriter)
                return
            kept = [h for h in rheaders if h.split(":", 1)[0].lower() not in ("connection", "keep-alive")]
            writer.write(("\r\n".join([status_line, *kept, "Connection: close"]) + "\r\n\r\n").encode("latin-1") + rbody)
            await writer.drain()
            while chunk := await ureader.read(65536):
                writer.write(chunk)
                await writer.drain()
        finally:
            uwriter.close()

    @staticmethod
    async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ureader: asyncio.StreamReader,
                      uwriter: asyncio.StreamWriter) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, half_close: bool) -> None:
            try:
                while chunk := await src.read(65536):
                    dst.write(chunk)
                    await dst.drain()
                if half_close and dst.can_write_eof():
                    dst.write_eof()                          # stdin reached EOF: tell the container, keep reading its output
            except (ConnectionError, OSError):
                pass

        up = asyncio.create_task(pump(reader, uwriter, True))
        down = asyncio.create_task(pump(ureader, writer, False))
        await down                                           # the container's output ended: the call is over
        up.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await up
