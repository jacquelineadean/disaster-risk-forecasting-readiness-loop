"""Provenance and transport.

These tests cover the two bugs that made it into a working build and had to be
fixed — a manifest that claimed provenance it did not have, and a connect path
that took six minutes to reach a host curl reached in 0.6 seconds — plus the
scope machinery: one national year file must serve any state, several states,
or the whole country, without a second download.
"""

import csv
import gzip
import io
import os
import pathlib
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from readiness.connectors import census, storm_events
from readiness.connectors.base import (
    CONNECT_TIMEOUT,
    ConnectorError,
    Manifest,
    Session,
    SourceRecord,
    _connect_socket,
    _proxy_auth,
    proxy_for,
    sha256_bytes,
    utc_now,
)


def record(sha: str = "a" * 64, **kw) -> SourceRecord:
    base = dict(
        source="test",
        url="https://example.invalid/f",
        sha256=sha,
        bytes=10,
        fetched_at=utc_now(),
        license="public domain",
    )
    base.update(kw)
    return SourceRecord(**base)


def year_file(rows: list[dict]) -> bytes:
    """A gzipped Storm Events CSV with exactly the columns the connector reads."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(storm_events._FIELDS) + ["EXTRA"])
    writer.writeheader()
    for row in rows:
        writer.writerow({**{f: "" for f in storm_events._FIELDS}, "EXTRA": "x", **row})
    return gzip.compress(buf.getvalue().encode())


def row(state: str, year: int = 2000, event_type: str = "Flood", cz: str = "1") -> dict:
    return {
        "EVENT_ID": f"{state}-{year}-{cz}",
        "YEAR": str(year),
        "BEGIN_YEARMONTH": f"{year}06",
        "EVENT_TYPE": event_type,
        "STATE_FIPS": state,
        "CZ_TYPE": "C",
        "CZ_FIPS": cz,
        "DAMAGE_PROPERTY": "10.00K",
    }


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "manifest.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_manifest_is_falsy(self):
        self.assertFalse(Manifest(path=self.path))

    def test_empty_manifest_does_not_produce_a_plausible_hash(self):
        # Regression: an empty manifest used to hash to sha256("") — a real
        # digest that silently claimed provenance for unpinned data, and got
        # written onto experiment cards as if it meant something.
        digest = Manifest(path=self.path).digest()
        self.assertEqual(digest, Manifest.UNPINNED)
        self.assertNotEqual(len(digest), 16)

    def test_digest_is_content_addressed(self):
        a = Manifest(path=self.path)
        a.add("x", record(sha="1" * 64))
        b = Manifest(path=self.path)
        b.add("x", record(sha="1" * 64))
        self.assertEqual(a.digest(), b.digest())

        c = Manifest(path=self.path)
        c.add("x", record(sha="2" * 64))
        self.assertNotEqual(a.digest(), c.digest())

    def test_digest_is_order_independent(self):
        a = Manifest(path=self.path)
        a.add("x", record(sha="1" * 64))
        a.add("y", record(sha="2" * 64))
        b = Manifest(path=self.path)
        b.add("y", record(sha="2" * 64))
        b.add("x", record(sha="1" * 64))
        self.assertEqual(a.digest(), b.digest())

    def test_digest_over_a_subset_of_keys(self):
        m = Manifest(path=self.path)
        m.add("x", record(sha="1" * 64))
        m.add("y", record(sha="2" * 64))
        only_x = Manifest(path=self.path)
        only_x.add("x", record(sha="1" * 64))
        # A panel built from x alone has the same data version whether or not
        # y happens to be pinned for some other contract.
        self.assertEqual(m.digest(["x"]), only_x.digest())
        self.assertNotEqual(m.digest(["x"]), m.digest())
        with self.assertRaises(Exception):
            m.digest(["x", "not-pinned"])

    def test_upstream_content_change_is_noted_not_hidden(self):
        m = Manifest(path=self.path)
        m.add("x", record(sha="1" * 64))
        m.add("x", record(sha="2" * 64))
        self.assertIn("upstream content changed", m.records["x"].notes)

    def test_round_trips_through_disk(self):
        m = Manifest(path=self.path)
        m.add("x", record())
        m.save()
        reloaded = Manifest.load(self.path)
        self.assertEqual(reloaded.digest(), m.digest())
        self.assertEqual(reloaded.records["x"].sha256, "a" * 64)

    def test_missing_file_loads_as_empty(self):
        self.assertFalse(Manifest.load(self.path))

    def test_save_is_a_no_op_until_something_is_pinned(self):
        m = Manifest(path=self.path)
        m.add("x", record())
        self.assertTrue(m.save())
        stamp = self.path.read_text()
        reloaded = Manifest.load(self.path)
        self.assertFalse(reloaded.save(), "nothing changed; the file must not be rewritten")
        self.assertEqual(self.path.read_text(), stamp)
        reloaded.add("y", record(sha="2" * 64))
        self.assertTrue(reloaded.save())
        self.assertTrue(reloaded.save(force=True))


class TestParseYear(unittest.TestCase):
    def setUp(self):
        self.data = year_file([row("1"), row("01", cz="3"), row("48"), row("56"), row("")])

    def test_groups_rows_by_padded_state_fips(self):
        grouped = storm_events.parse_year(self.data, None)
        self.assertEqual(sorted(grouped), ["01", "48", "56"])
        self.assertEqual(len(grouped["01"]), 2)  # "1" and "01" are the same state

    def test_filters_to_requested_states(self):
        stats: dict = {}
        grouped = storm_events.parse_year(self.data, ["48", "56"], stats)
        self.assertEqual(sorted(grouped), ["48", "56"])
        self.assertEqual(stats["n_total"], 4)  # the national count, not the filtered one

    def test_schema_drift_is_an_error(self):
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=["EVENT_ID", "YEAR"]).writeheader()
        with self.assertRaises(storm_events.ConnectorError) as ctx:
            storm_events.parse_year(gzip.compress(buf.getvalue().encode()), None)
        self.assertIn("schema drift", str(ctx.exception))


class TestScopeLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(storm_events.scope_label(None), "all")
        self.assertEqual(storm_events.scope_label(["22"]), "22")
        self.assertEqual(storm_events.scope_label(["48", "22", "22"]), "22+48")
        self.assertEqual(storm_events.scope_label(["1"]), "01")


class SnapshotCase(unittest.TestCase):
    """Snapshot against a fake NCEI: `fetch` returns synthetic year files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.files = {
            2000: year_file([row("48", 2000), row("56", 2000), row("22", 2000)]),
            2001: year_file([row("48", 2001), row("56", 2001, cz="7")]),
        }
        self.downloads: list[int] = []

        def fake_discover(years):
            return {y: f"https://ncei.invalid/{y}.csv.gz" for y in years}

        def fake_fetch(url, **_kw):
            year = int(url.rsplit("/", 1)[1].split(".")[0])
            self.downloads.append(year)
            return self.files[year]

        self.patches = [
            mock.patch.object(storm_events, "discover_files", fake_discover),
            mock.patch.object(storm_events, "fetch", fake_fetch),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def snapshot(self, states, **kw):
        return storm_events.snapshot([2000, 2001], states, self.dir, self.manifest, **kw)


class TestSnapshotScopes(SnapshotCase):
    def test_single_state_extract(self):
        out = self.snapshot(["48"])
        events = storm_events.load_events(out)
        self.assertEqual({e.state_fips for e in events}, {"48"})
        self.assertEqual(len(events), 2)
        self.assertEqual(sorted(self.downloads), [2000, 2001])
        self.assertIn("noaa/storm_events/2000", self.manifest.records)
        self.assertEqual(
            self.manifest.records["noaa/storm_events/2000"].sha256,
            sha256_bytes(self.files[2000]),
        )

    def test_multi_state_extract_is_the_union(self):
        out = self.snapshot(["48", "56"])
        events = storm_events.load_events(out)
        self.assertEqual({e.state_fips for e in events}, {"48", "56"})
        self.assertEqual(out.name, "48+56_extract.jsonl")

    def test_national_extract_keeps_every_state(self):
        out = self.snapshot(None)
        events = storm_events.load_events(out)
        self.assertEqual({e.state_fips for e in events}, {"22", "48", "56"})
        self.assertEqual(out.name, "all_extract.jsonl")
        self.assertIn("3 events nationally", self.manifest.records["noaa/storm_events/2000"].notes)

    def test_state_scoped_pulls_still_record_the_national_count(self):
        self.snapshot(["48"])
        self.assertIn("3 events nationally", self.manifest.records["noaa/storm_events/2000"].notes)

    def test_a_second_state_reuses_kept_raw_files_without_downloading(self):
        self.snapshot(["48"], keep_raw=True)
        self.assertEqual(sorted(self.downloads), [2000, 2001])
        self.downloads.clear()
        out = self.snapshot(["56"])
        self.assertEqual(self.downloads, [], "pinned raw bytes should have been re-used")
        self.assertEqual({e.state_fips for e in storm_events.load_events(out)}, {"56"})

    def test_a_kept_raw_file_that_no_longer_matches_the_manifest_is_not_trusted(self):
        self.snapshot(["48"], keep_raw=True)
        (self.dir / "storm_events_raw" / "2000.csv.gz").write_bytes(b"tampered")
        self.downloads.clear()
        self.snapshot(["56"])
        self.assertEqual(sorted(self.downloads), [2000])

    def test_pinned_extracts_are_not_refetched(self):
        self.snapshot(["48"])
        self.downloads.clear()
        self.snapshot(["48"])
        self.assertEqual(self.downloads, [])

    def test_refresh_refetches(self):
        self.snapshot(["48"])
        self.downloads.clear()
        self.snapshot(["48"], refresh=True)
        self.assertEqual(sorted(self.downloads), [2000, 2001])

    def test_a_reprocessed_year_recuts_every_existing_extract(self):
        # State 48 is pulled while NCEI serves one version of 2000; then the
        # upstream file changes and state 56 is pulled. The manifest now pins
        # the new bytes for 2000, so 48's extract must be re-cut from them
        # too — otherwise it stays stale under a record that says otherwise.
        self.snapshot(["48"])
        self.files[2000] = year_file(
            [row("48", 2000), row("48", 2000, cz="9"), row("56", 2000), row("22", 2000)]
        )
        self.files[2001] = year_file([row("48", 2001), row("56", 2001)])
        self.snapshot(["56"])
        events_48 = storm_events.load_events(self.dir / "storm_events" / "48_2000.jsonl")
        self.assertEqual(len(events_48), 2, "48's extract was not re-cut from the new bytes")
        self.assertIn(
            "upstream content changed", self.manifest.records["noaa/storm_events/2000"].notes
        )


class TestUnpinnedCacheIsRefetched(unittest.TestCase):
    """A cached extract with no manifest record must not be trusted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "storm_events").mkdir(parents=True)
        # Simulate the state left by a download that completed but crashed
        # before the manifest was saved: bytes on disk, no provenance.
        for year in (2000, 2001):
            (self.dir / "storm_events" / f"48_{year}.jsonl").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_without_manifest_record_is_treated_as_missing(self):
        manifest = Manifest(path=self.dir / "manifest.json")
        calls: list[int] = []

        def fake_discover(years):
            calls.extend(years)
            raise storm_events.ConnectorError("stop here; we only need the years")

        with mock.patch.object(storm_events, "discover_files", fake_discover):
            with self.assertRaises(storm_events.ConnectorError):
                storm_events.snapshot([2000, 2001], ["48"], self.dir, manifest)
        self.assertEqual(sorted(calls), [2000, 2001])

    def test_extract_with_manifest_record_is_reused(self):
        manifest = Manifest(path=self.dir / "manifest.json")
        for year in (2000, 2001):
            manifest.add(f"noaa/storm_events/{year}", record())

        def fail(_years):
            raise AssertionError("should not re-download a pinned extract")

        with mock.patch.object(storm_events, "discover_files", fail):
            out = storm_events.snapshot([2000, 2001], ["48"], self.dir, manifest)
        self.assertTrue(out.exists())


class TestCensus(unittest.TestCase):
    DATA = (
        "STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME|CLASSFP|FUNCSTAT\n"
        "AA|97|001|00000001|Alpha County|H1|A\n"
        "AA|97|003|00000002|Beta County|H1|A\n"
        "BB|98|001|00000003|Gamma County|H1|A\n"
    ).encode()

    def test_parse(self):
        counties = census.parse(self.DATA)
        self.assertEqual([c.fips for c in counties], ["97001", "97003", "98001"])

    def test_for_states_filters_and_sorts(self):
        counties = census.parse(self.DATA)
        self.assertEqual([c.fips for c in census.for_states(counties, ["bb"])], ["98001"])
        self.assertEqual(len(census.for_states(counties, ["AA", "BB"])), 3)

    def test_empty_scope_is_national(self):
        counties = census.parse(self.DATA)
        self.assertEqual(len(census.for_states(counties, [])), 3)

    def test_unknown_state_is_an_error(self):
        counties = census.parse(self.DATA)
        with self.assertRaises(KeyError):
            census.for_states(counties, ["ZZ"])


class TestCensusCache(unittest.TestCase):
    """A cached county file is trusted only when it hashes to its manifest record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "census"
        self.manifest = Manifest(path=pathlib.Path(self.tmp.name) / "manifest.json")
        self.fetches = 0

        def fake_fetch(_url, **_kw):
            self.fetches += 1
            return TestCensus.DATA

        self.patch = mock.patch.object(census, "fetch", fake_fetch)
        self.patch.start()
        census.load(self.dir, self.manifest)
        self.cache = self.dir / "national_county2020.txt"
        self.fetches = 0

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_pinned_cache_is_reused(self):
        census.load(self.dir, self.manifest)
        self.assertEqual(self.fetches, 0)

    def test_cache_that_no_longer_matches_the_manifest_is_refetched(self):
        # Same bug class as the storm_events raw-file check: a record existed,
        # so the bytes were trusted without ever being hashed.
        self.cache.write_bytes(b"STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME\nZZ|99|001|1|X\n")
        counties = census.load(self.dir, self.manifest)
        self.assertEqual(self.fetches, 1)
        self.assertEqual([c.fips for c in counties], ["97001", "97003", "98001"])
        self.assertEqual(self.cache.read_bytes(), TestCensus.DATA)

    def test_mismatch_is_an_error_when_fetching_is_not_allowed(self):
        self.cache.write_bytes(b"tampered")
        with self.assertRaises(ConnectorError) as ctx:
            census.load(self.dir, self.manifest, allow_fetch=False)
        msg = str(ctx.exception)
        self.assertIn(str(self.cache), msg)
        self.assertIn(sha256_bytes(b"tampered"), msg)
        self.assertIn(sha256_bytes(TestCensus.DATA), msg)
        self.assertEqual(self.fetches, 0)

    def test_unpinned_cache_is_an_error_when_fetching_is_not_allowed(self):
        self.manifest.records.clear()
        with self.assertRaises(ConnectorError):
            census.load(self.dir, self.manifest, allow_fetch=False)
        self.assertEqual(self.fetches, 0)

    def test_pinned_cache_is_fine_when_fetching_is_not_allowed(self):
        census.load(self.dir, self.manifest, allow_fetch=False)
        self.assertEqual(self.fetches, 0)


class _FakeProxyHandler(socketserver.StreamRequestHandler):
    """Records the request line it receives, then answers like an origin.

    A CONNECT is acknowledged and the next request line on the same socket —
    the one the client believes it is sending through the tunnel — is
    recorded too. TLS is patched out by the tests, so the "tunnel" carries
    plain HTTP, which is enough to prove what was sent to whom.
    """

    BODY = b"hello via proxy"

    def _read_request(self) -> str:
        line = self.rfile.readline().decode().rstrip("\r\n")
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        return line

    def handle(self):
        line = self._read_request()
        self.server.lines.append(line)
        if line.startswith("CONNECT "):
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            self.server.lines.append(self._read_request())
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(self.BODY)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + self.BODY
        )
        self.wfile.flush()


class _FakeProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeProxyHandler)
        self.lines: list[str] = []
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


class TestProxy(unittest.TestCase):
    """`Session` honours HTTP(S)_PROXY / NO_PROXY the way urllib does."""

    def setUp(self):
        self.proxy = _FakeProxy()
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        # The fake proxy cannot terminate TLS; the origin it "reaches" is the
        # fake itself. Record what SNI the client asked for and hand the plain
        # socket back so the tunnel carries readable HTTP.
        self.sni: list[str | None] = []

        def no_tls(_ctx, sock, **kw):
            self.sni.append(kw.get("server_hostname"))
            return sock

        self.tls = mock.patch.object(ssl.SSLContext, "wrap_socket", no_tls)
        self.tls.start()

    def tearDown(self):
        self.tls.stop()
        self.env.stop()
        self.proxy.stop()

    def test_https_goes_through_a_connect_tunnel(self):
        os.environ["HTTPS_PROXY"] = self.proxy.url
        body = Session(retries=1).get("https://example.invalid/data/file.txt?x=1")
        self.assertEqual(body, _FakeProxyHandler.BODY)
        # http.client writes the CONNECT line as HTTP/1.0 on 3.11 and 1.1 on
        # 3.12; the target is what matters.
        connect, inner = self.proxy.lines
        self.assertTrue(connect.startswith("CONNECT example.invalid:443 HTTP/1."))
        self.assertEqual(inner, "GET /data/file.txt?x=1 HTTP/1.1")
        self.assertEqual(self.sni, ["example.invalid"], "TLS must name the origin")

    def test_http_sends_the_absolute_uri(self):
        os.environ["HTTP_PROXY"] = self.proxy.url
        body = Session(retries=1).get("http://example.invalid/index.html?a=b")
        self.assertEqual(body, _FakeProxyHandler.BODY)
        self.assertEqual(
            self.proxy.lines, ["GET http://example.invalid/index.html?a=b HTTP/1.1"]
        )
        self.assertEqual(self.sni, [])

    def test_no_proxy_host_is_dialled_directly(self):
        # The proxy is also the "origin" here; with the host excluded the
        # request must reach it as a plain origin request, not a proxy one.
        os.environ["HTTP_PROXY"] = "http://192.0.2.1:9"  # unroutable: must be skipped
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        body = Session(retries=1).get(f"{self.proxy.url}/direct")
        self.assertEqual(body, _FakeProxyHandler.BODY)
        self.assertEqual(self.proxy.lines, ["GET /direct HTTP/1.1"])

    def test_no_proxy_environment_keeps_the_direct_path(self):
        body = Session(retries=1).get(f"{self.proxy.url}/direct")
        self.assertEqual(body, _FakeProxyHandler.BODY)
        self.assertEqual(self.proxy.lines, ["GET /direct HTTP/1.1"])

    def test_proxy_credentials_become_a_proxy_authorization_header(self):
        os.environ["HTTP_PROXY"] = "http://u:p%40w@proxy.invalid:3128"
        proxy = proxy_for("http", "example.invalid")
        self.assertEqual(proxy.username, "u")
        self.assertEqual(_proxy_auth(proxy), {"Proxy-Authorization": "Basic dTpwQHc="})

    def test_proxy_for_resolves_the_environment(self):
        self.assertIsNone(proxy_for("https", "example.invalid"))
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:3128"
        os.environ["NO_PROXY"] = ".noaa.gov"
        self.assertEqual(proxy_for("https", "example.invalid").port, 3128)
        self.assertIsNone(proxy_for("https", "www.ncei.noaa.gov"))
        self.assertIsNone(proxy_for("http", "example.invalid"))


class TestConnect(unittest.TestCase):
    def test_per_address_timeout_is_short(self):
        # The whole point: a dead address must cost seconds, not the full
        # request timeout, so a host with six unreachable AAAA records does not
        # cost six full timeouts before the first IPv4 address is tried.
        self.assertLessEqual(CONNECT_TIMEOUT, 10.0)

    def test_unreachable_host_fails_with_a_useful_message(self):
        with self.assertRaises(Exception) as ctx:
            # TEST-NET-1 (RFC 5737): guaranteed not routable.
            _connect_socket("192.0.2.1", 9, timeout=1.0)
        self.assertIn("could not connect", str(ctx.exception))

    def test_loopback_connects(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            sock = _connect_socket("127.0.0.1", port, timeout=5.0)
            self.assertIsNotNone(sock)
            sock.close()
        finally:
            server.close()


class TestDamageColumnContract(unittest.TestCase):
    def test_required_columns_are_declared(self):
        # Schema drift in Storm Events must be caught at parse time, not become
        # silently missing labels. These are the columns the project depends on.
        for col in (
            "EVENT_TYPE",
            "STATE_FIPS",
            "CZ_TYPE",
            "CZ_FIPS",
            "DAMAGE_PROPERTY",
            "INJURIES_DIRECT",
            "DEATHS_DIRECT",
        ):
            self.assertIn(col, storm_events._FIELDS)


if __name__ == "__main__":
    unittest.main()
