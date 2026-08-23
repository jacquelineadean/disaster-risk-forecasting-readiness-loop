"""Snapshot machinery shared by every connector.

Report §5: "Each snapshots raw pulls with checksums, so every experiment is
reproducible against a pinned data version." Report §7: "Snapshot every
dependency with checksums, mirror what licenses allow, and design connectors so
a community-maintained substitute can slot in."

The manifest is the artefact that matters and the one that gets committed. Raw
downloads are large and are deleted after extraction unless `keep_raw` is set;
the checksum in the manifest is what pins the experiment, not the bytes on this
particular disk.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import http.client
import json
import pathlib
import socket
import threading
import urllib.parse
from dataclasses import asdict, dataclass, field

USER_AGENT = (
    "readiness-loop/0.1 (open-source disaster-risk research; "
    "https://github.com/ - contact via repo issues)"
)


class ConnectorError(RuntimeError):
    pass


@dataclass
class SourceRecord:
    """One pinned file: where it came from, what it hashed to, when."""

    source: str
    url: str
    sha256: str
    bytes: int
    fetched_at: str
    license: str
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Manifest:
    """The committed record of every raw pull backing a snapshot."""

    path: pathlib.Path
    records: dict[str, SourceRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: pathlib.Path) -> "Manifest":
        if not path.exists():
            return cls(path=path)
        raw = json.loads(path.read_text())
        return cls(
            path=path,
            records={k: SourceRecord(**v) for k, v in raw.get("records", {}).items()},
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "generated_at": utc_now(),
            "records": {k: v.to_dict() for k, v in sorted(self.records.items())},
        }
        self.path.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n")

    def add(self, key: str, record: SourceRecord) -> None:
        prior = self.records.get(key)
        if prior and prior.sha256 != record.sha256:
            record.notes = (
                f"upstream content changed (was sha256:{prior.sha256[:12]}...); "
                "prior experiments were run against the old bytes"
            ).strip()
        self.records[key] = record

    #: Returned instead of a hash when nothing is pinned. Must not look like a
    #: digest: an empty manifest previously hashed to sha256("") — a perfectly
    #: plausible-looking value that silently claimed provenance for data that
    #: had none, and got written onto experiment cards as if it meant something.
    UNPINNED = "UNPINNED"

    def __bool__(self) -> bool:
        return bool(self.records)

    def digest(self) -> str:
        """A single hash over every pinned file — one id for a data version."""
        if not self.records:
            return self.UNPINNED
        h = hashlib.sha256()
        for key, rec in sorted(self.records.items()):
            h.update(f"{key}|{rec.sha256}\n".encode())
        return h.hexdigest()[:16]

    def summary(self) -> str:
        if not self.records:
            return "no snapshots recorded"
        total = sum(r.bytes for r in self.records.values())
        lines = [
            f"{len(self.records)} pinned file(s), {total / 1e6:.1f} MB, "
            f"data version sha256:{self.digest()}"
        ]
        for key, rec in sorted(self.records.items()):
            note = f"  ({rec.notes})" if rec.notes else ""
            lines.append(
                f"  {key:<28} sha256:{rec.sha256[:16]}  "
                f"{rec.bytes / 1e6:>7.1f} MB  {rec.fetched_at}{note}"
            )
        return "\n".join(lines)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Per-address connect timeout. Short on purpose — see `_connect_socket`.
CONNECT_TIMEOUT = 5.0

#: Remembers which address family answered for a host, so later connections in
#: the same process skip a family that has already proved dead.
_WORKING_FAMILY: dict[str, int] = {}
_FAMILY_LOCK = threading.Lock()


def _connect_socket(host: str, port: int, timeout: float | None) -> socket.socket:
    """Open a TCP connection, trying each resolved address in turn.

    `socket.create_connection` walks the `getaddrinfo` list in order and applies
    the *full* timeout to each address. When a host publishes AAAA records that
    are unreachable — an IPv6 blackhole, common on corporate networks, in
    containers, and behind filtered egress — that means one full timeout of dead
    waiting per IPv6 address before the first IPv4 address is even tried.
    NOAA's NCEI publishes six AAAA records, so a 60-second default turns into
    six minutes of nothing before the first byte.

    curl solves this with Happy Eyeballs. This is the small version: a short
    per-address timeout, and a memo of which family actually worked so the
    second request through this process does not re-learn it.
    """
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    with _FAMILY_LOCK:
        preferred = _WORKING_FAMILY.get(host)
    if preferred is not None:
        infos.sort(key=lambda i: 0 if i[0] == preferred else 1)

    per_address = CONNECT_TIMEOUT if timeout is None else min(CONNECT_TIMEOUT, timeout)
    errors: list[str] = []
    for family, socktype, proto, _canon, addr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(per_address)
            sock.connect(addr)
        except OSError as exc:
            sock.close()
            errors.append(f"{addr[0]}: {type(exc).__name__}")
            continue
        sock.settimeout(timeout)
        with _FAMILY_LOCK:
            _WORKING_FAMILY[host] = family
        return sock
    raise ConnectorError(
        f"could not connect to {host}:{port}; tried "
        f"{len(infos)} address(es): {'; '.join(errors)}"
    )


class _HappyHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_socket(self.host, self.port, self.timeout)
        if self._tunnel_host:
            self._tunnel()


class _HappyHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        self.sock = _connect_socket(self.host, self.port, self.timeout)
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname
        )


class Session:
    """HTTPS session with keep-alive, one persistent connection per thread.

    A Storm Events pull is ~30 files from a single host. Opening a fresh TCP
    connection and TLS handshake for each one is wasteful everywhere and
    crippling behind a slow or filtered egress path, where connection setup can
    dominate transfer time by two orders of magnitude. Reusing one connection
    per worker amortises that cost across the whole pull.
    """

    def __init__(self, timeout: int = 180, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()

    def _connections(self) -> dict[tuple[str, str], http.client.HTTPConnection]:
        if not hasattr(self._local, "conns"):
            self._local.conns = {}
        return self._local.conns

    def _connect(self, scheme: str, host: str) -> http.client.HTTPConnection:
        cls = _HappyHTTPSConnection if scheme == "https" else _HappyHTTPConnection
        return cls(host, timeout=self.timeout)

    def _request(self, url: str) -> tuple[int, dict, bytes]:
        parts = urllib.parse.urlsplit(url)
        key = (parts.scheme, parts.netloc)
        conns = self._connections()
        conn = conns.get(key)
        if conn is None:
            conn = conns[key] = self._connect(parts.scheme, parts.netloc)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(
            "GET",
            path,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            },
        )
        resp = conn.getresponse()
        body = resp.read()  # must drain before the connection can be reused
        return resp.status, dict(resp.getheaders()), body

    def get(self, url: str, *, _redirects: int = 0) -> bytes:
        if _redirects > 5:
            raise ConnectorError(f"too many redirects fetching {url}")
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                status, headers, body = self._request(url)
            except (http.client.HTTPException, OSError) as exc:
                # A stale keep-alive connection raises on reuse; drop it and retry.
                last = exc
                self.close()
                continue
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location") or headers.get("location")
                if not location:
                    raise ConnectorError(f"HTTP {status} for {url} with no Location")
                return self.get(
                    urllib.parse.urljoin(url, location), _redirects=_redirects + 1
                )
            if status != 200:
                raise ConnectorError(f"HTTP {status} for {url}")
            return body
        raise ConnectorError(
            f"failed to fetch {url} after {self.retries} attempt(s): {last}\n"
            "If this source has moved or been retired, that is exactly the "
            "continuity risk in report §7 — record it and slot in a mirror."
        )

    def close(self) -> None:
        for conn in self._connections().values():
            try:
                conn.close()
            except Exception:
                pass
        self._local.conns = {}


#: Process-wide default session. Connectors may pass their own.
DEFAULT_SESSION = Session()


def fetch(
    url: str, *, timeout: int = 180, retries: int = 3, session: Session | None = None
) -> bytes:
    """Download a URL over a keep-alive session. Raises `ConnectorError` on give-up."""
    if session is not None:
        return session.get(url)
    if timeout != DEFAULT_SESSION.timeout or retries != DEFAULT_SESSION.retries:
        return Session(timeout=timeout, retries=retries).get(url)
    return DEFAULT_SESSION.get(url)
