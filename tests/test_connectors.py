"""Provenance and transport.

These tests cover the two bugs that made it into a working build and had to be
fixed — a manifest that claimed provenance it did not have, and a connect path
that took six minutes to reach a host curl reached in 0.6 seconds — plus the
scope machinery: one national year file must serve any state, several states,
or the whole country, without a second download.
"""

import ast
import calendar
import csv
import gzip
import io
import json
import math
import os
import pathlib
import re
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from readiness.connectors import (
    CONNECTORS,
    ConnectorInfo,
    census,
    climada_layer,
    connector_for_key,
    gazetteer,
    nri,
    nws_zones,
    open_meteo,
    storm_events,
    usa_structures,
)
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
from readiness.contracts import Contract
from readiness.harness import features as F
from tests.fixtures import make_contract

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "tests" / "data"


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
        self.assertIn("1" * 12, m.records["x"].notes)

    def test_the_change_notice_is_appended_to_the_connectors_own_notes(self):
        # `backtest` reads `derived_through=YYYY` out of a record's notes to
        # decide whether a static layer may be a feature; a notice that
        # replaced them would strip a re-pulled layer of its vintage.
        m = Manifest(path=self.path)
        m.add("nri", record(sha="1" * 64, notes="derived_through=2023; v1.20"))
        m.add("nri", record(sha="2" * 64, notes="derived_through=2023; v1.20"))
        notes = m.records["nri"].notes
        self.assertTrue(notes.startswith("derived_through=2023; v1.20"), notes)
        self.assertIn("upstream content changed", notes)
        # A connector that writes no notes still gets the notice alone.
        m.add("bare", record(sha="1" * 64, notes=""))
        m.add("bare", record(sha="2" * 64, notes=""))
        self.assertTrue(m.records["bare"].notes.startswith("upstream content changed"))

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



# ---------------------------------------------------------------------------
# The registry as data
# ---------------------------------------------------------------------------


class TestRegistry(unittest.TestCase):
    def test_every_key_the_connectors_write_resolves(self):
        keys = {
            "census": "census/national_county2020",
            "storm_events": "noaa/storm_events/2010",
            "nws_zones": nws_zones.MANIFEST_KEY,
            "gazetteer": gazetteer.MANIFEST_KEY,
            "era5": open_meteo.manifest_key("22"),
            "nri": nri.MANIFEST_KEY,
            "climada": climada_layer.manifest_key("inland_flood", "US:LA"),
            "usa_structures": usa_structures.manifest_key("22"),
        }
        self.assertEqual(sorted(keys), sorted(CONNECTORS))
        for name, key in keys.items():
            self.assertIs(connector_for_key(key), CONNECTORS[name], key)

    def test_the_registry_literals_match_the_connectors_constants(self):
        # The registry cannot import its modules (see its docstring), so the
        # licences are literals; this is the tripwire that keeps them honest.
        licences = {
            "census": census.LICENSE,
            "storm_events": storm_events.LICENSE,
            "nws_zones": nws_zones.LICENSE,
            "gazetteer": gazetteer.LICENSE,
            "era5": open_meteo.LICENSE,
            "nri": nri.LICENSE,
            "climada": climada_layer.LICENSE,
            "usa_structures": usa_structures.LICENSE,
        }
        for name, licence in licences.items():
            self.assertEqual(CONNECTORS[name].license, licence, name)
        for name, module in (
            ("gazetteer", gazetteer), ("era5", open_meteo),
            ("nri", nri), ("climada", climada_layer),
            ("usa_structures", usa_structures),
        ):
            self.assertEqual(CONNECTORS[name].source, module.SOURCE, name)

    def test_the_package_init_imports_none_of_its_modules(self):
        tree = ast.parse((REPO_ROOT / "readiness" / "connectors" / "__init__.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("readiness."), node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("readiness."), alias.name)

    def test_an_unknown_key_is_an_error(self):
        with self.assertRaises(KeyError):
            connector_for_key("somebody/else")

    def test_longest_prefix_wins(self):
        short = ConnectorInfo("a/", "s", "l", False, False, False)
        long = ConnectorInfo("a/b/", "s", "l", False, False, False)
        registry = {"short": short, "long": long}
        self.assertIs(connector_for_key("a/b/c", registry), long)
        self.assertIs(connector_for_key("a/x", registry), short)

    def test_the_ground_truth_is_the_only_label_source(self):
        labels = [k for k, v in CONNECTORS.items() if v.is_label_source]
        self.assertEqual(labels, ["storm_events"])
        # ...and the harness's label-origin rule already knows its prefix.
        self.assertTrue(
            CONNECTORS["storm_events"].key_prefix.startswith(F.LABEL_SOURCE_PREFIXES)
        )

    def test_only_the_climada_layer_needs_no_network(self):
        offline = sorted(k for k, v in CONNECTORS.items() if not v.network)
        self.assertEqual(offline, ["climada"])

    def test_the_climada_tool_is_never_imported_by_the_package(self):
        # The tool is GPL-3.0 territory; the package names it in prose and
        # error messages only. Checked on the AST, like `test_boundaries`.
        for file in (REPO_ROOT / "readiness").rglob("*.py"):
            for node in ast.walk(ast.parse(file.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    forbidden = name.startswith("tools") or "run_event_set" in name
                    self.assertFalse(forbidden, f"{file}: {name}")


class TestLicenceManifest(unittest.TestCase):
    def test_every_registered_connector_has_a_licence_row(self):
        text = (REPO_ROOT / "DATA-LICENSES.md").read_text()
        for name, info in CONNECTORS.items():
            self.assertIn(f"`{info.key_prefix}`", text, f"{name} has no licence row")


# ---------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------


def gazetteer_zip(text: bytes) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2020_Gaz_counties_national.txt", text)
    return buf.getvalue()


class TestGazetteer(unittest.TestCase):
    sample = (DATA / "gazetteer_sample.txt").read_bytes()

    def test_parse_the_committed_sample(self):
        centroids = gazetteer.parse(self.sample)
        self.assertEqual(sorted(centroids), ["98001", "99001", "99003", "99005", "99007"])
        c = centroids["99001"]
        self.assertEqual((c.lat, c.lon), (30.1234567, -91.1234567))
        self.assertEqual((c.land_m2, c.water_m2), (2.0e9, 5.0e8))
        self.assertAlmostEqual(c.water_share, 0.2)

    def test_parse_reads_the_zip_the_census_ships(self):
        zipped = gazetteer.parse(gazetteer_zip(self.sample))
        self.assertEqual(zipped, gazetteer.parse(self.sample))

    def test_schema_drift_is_an_error(self):
        drifted = self.sample.replace(b"INTPTLONG", b"LONGITUDE")
        with self.assertRaises(ConnectorError):
            gazetteer.parse(drifted)
        with self.assertRaises(ConnectorError):
            gazetteer.parse(b"USPS\tGEOID\tALAND\tAWATER\tINTPTLAT\tINTPTLONG\n")

    def test_source_is_timeless_geometry_and_admitted(self):
        src = gazetteer.source(gazetteer.parse(self.sample))
        self.assertEqual((src.name, src.kind), ("gazetteer", "static"))
        self.assertIsNone(src.derived_through)
        self.assertEqual(src.manifest_keys, ("census/gazetteer_counties2020",))
        F.admit(src, make_contract())
        table = src.static("99005")
        self.assertEqual(sorted(table), ["land_km2", "lat", "lon", "water_share"])
        self.assertAlmostEqual(table["water_share"], 0.1)
        self.assertEqual(table["land_km2"], 900.0)
        self.assertIsNone(src.static("00000"))
        self.assertIsNone(src.series("99005", "precip_mm"))

    def test_water_share_of_a_zero_area_row_is_missing_not_zero(self):
        c = gazetteer.Centroid("x", 0.0, 0.0, 0.0, 0.0)
        self.assertTrue(math.isnan(c.water_share))

    def test_points_are_the_shape_open_meteo_asks_for(self):
        pts = gazetteer.points(gazetteer.parse(self.sample))
        self.assertEqual(pts["99003"], (30.5, -90.5))


class TestGazetteerCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.sample = (DATA / "gazetteer_sample.txt").read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetches_pins_and_then_reuses_the_cache(self):
        fetched = []

        def fake_fetch(url, **_kw):
            fetched.append(url)
            return gazetteer_zip(self.sample)

        with mock.patch.object(gazetteer, "fetch", fake_fetch):
            first = gazetteer.load(self.dir, self.manifest)
            second = gazetteer.load(self.dir, self.manifest)
        self.assertEqual(fetched, [gazetteer.URL])
        self.assertEqual(first, second)
        rec = self.manifest.records[gazetteer.MANIFEST_KEY]
        self.assertEqual(rec.sha256, sha256_bytes(gazetteer_zip(self.sample)))
        self.assertEqual(rec.license, gazetteer.LICENSE)

    def test_unpinned_cache_is_an_error_when_fetching_is_not_allowed(self):
        (self.dir / gazetteer.CACHE_NAME).write_bytes(self.sample)
        with self.assertRaises(ConnectorError):
            gazetteer.load(self.dir, self.manifest, allow_fetch=False)


# ---------------------------------------------------------------------------
# Open-Meteo ERA5
# ---------------------------------------------------------------------------


class FakeSession:
    """`get(url)` returns the committed payload; optionally 429s first."""

    def __init__(self, payload: bytes, rate_limit_first: int = 0):
        self.payload = payload
        self.urls: list[str] = []
        self.remaining_429 = rate_limit_first

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if self.remaining_429 > 0:
            self.remaining_429 -= 1
            raise ConnectorError(f"HTTP 429 for {url}")
        return self.payload


class RangeSession:
    """Serves a synthetic daily payload covering exactly the range the URL asks for.

    The committed sample spans two calendar years; the connector now asks for
    three, because the first period's trailing twelve-month window sits two
    years back. A resume test needs a source that answers whatever range it is
    handed, or every county looks uncovered and is fetched again forever.
    """

    def __init__(self, elevation: float = 12.0, daily_mm: float = 1.0):
        self.urls: list[str] = []
        self.elevation = elevation
        self.daily_mm = daily_mm

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        first = re.search(r"start_date=(\d{4})-(\d{2})", url)
        last = re.search(r"end_date=(\d{4})-(\d{2})", url)
        days, precip, temp = [], [], []
        start = F.month_index(int(first.group(1)), int(first.group(2)))
        end = F.month_index(int(last.group(1)), int(last.group(2)))
        for index in range(start, end + 1):
            year, month = divmod(index, 12)
            for day in range(1, calendar.monthrange(year, month + 1)[1] + 1):
                days.append(f"{year:04d}-{month + 1:02d}-{day:02d}")
                precip.append(self.daily_mm)
                temp.append(10.0)
        return json.dumps({
            "elevation": self.elevation,
            "daily": {"time": days, "precipitation_sum": precip,
                      "temperature_2m_mean": temp},
        }).encode()


class TestOpenMeteoParse(unittest.TestCase):
    payload = json.loads((DATA / "era5_sample.json").read_text())

    def test_monthly_aggregation_checked_by_hand(self):
        m = open_meteo.parse_daily_to_monthly(self.payload)
        self.assertEqual(m.month0, F.month_index(2004, 1))
        self.assertEqual(len(m.precip_mm), 24)
        self.assertEqual(len(m.tmean_c), 24)
        # January 2004: 31 days of 1.5 mm, temperatures 1..31.
        self.assertEqual(m.precip_mm[0], 46.5)
        self.assertEqual(m.tmean_c[0], 16.0)
        # February 2004 (leap year): 29 days of 0.5 mm, temperatures 1..29.
        self.assertEqual(m.precip_mm[1], 14.5)
        self.assertEqual(m.tmean_c[1], 15.0)
        self.assertEqual(m.elevation_m, 12.0)

    def test_a_month_with_a_null_day_is_missing_never_smaller(self):
        m = open_meteo.parse_daily_to_monthly(self.payload)
        march_2005 = F.month_index(2005, 3) - m.month0
        self.assertTrue(math.isnan(m.precip_mm[march_2005]))
        self.assertTrue(math.isnan(m.tmean_c[march_2005]))
        self.assertFalse(math.isnan(m.precip_mm[march_2005 + 1]))

    def test_a_partial_month_is_missing(self):
        daily = self.payload["daily"]
        cut = {**self.payload, "daily": {k: v[:40] for k, v in daily.items()}}
        m = open_meteo.parse_daily_to_monthly(cut)
        self.assertEqual(len(m.precip_mm), 2)
        self.assertEqual(m.precip_mm[0], 46.5)
        self.assertTrue(math.isnan(m.precip_mm[1]))  # 9 days of February

    def test_schema_drift_is_an_error(self):
        daily = dict(self.payload["daily"])
        daily["precip"] = daily.pop("precipitation_sum")
        with self.assertRaises(ConnectorError):
            open_meteo.parse_daily_to_monthly({**self.payload, "daily": daily})
        without_elevation = {k: v for k, v in self.payload.items() if k != "elevation"}
        with self.assertRaises(ConnectorError):
            open_meteo.parse_daily_to_monthly(without_elevation)
        with self.assertRaises(ConnectorError):
            open_meteo.parse_daily_to_monthly({"hourly": {}})
        short = {**self.payload["daily"], "temperature_2m_mean": [1.0]}
        with self.assertRaises(ConnectorError):
            open_meteo.parse_daily_to_monthly({**self.payload, "daily": short})


class TestOpenMeteoSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.payload = (DATA / "era5_sample.json").read_bytes()
        self.session = FakeSession(self.payload)
        self.sleeps: list[float] = []
        self.centroids = {"99003": (30.5, -90.5), "99001": (30.1, -91.1)}

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, centroids=None, **kw):
        kw.setdefault("session", self.session)
        kw.setdefault("sleep", self.sleeps.append)
        return open_meteo.snapshot(
            centroids or self.centroids, [2005], self.dir, self.manifest, **kw
        )

    def test_one_extract_per_state_pinned_by_its_own_bytes(self):
        paths = self.snapshot()
        self.assertEqual(paths, [self.dir / "open_meteo" / "99_era5_monthly.jsonl"])
        lines = [json.loads(ln) for ln in paths[0].read_text().splitlines()]
        self.assertEqual([ln["id"] for ln in lines], ["99001", "99003"])  # sorted
        self.assertEqual(
            sorted(lines[0]), ["elevation_m", "id", "month0", "precip_mm", "tmean_c"]
        )
        self.assertEqual(lines[0]["month0"], F.month_index(2004, 1))
        self.assertEqual(lines[0]["precip_mm"][0], 46.5)
        self.assertIsNone(lines[0]["precip_mm"][14])  # March 2005: null, not NaN
        rec = self.manifest.records["open-meteo/era5/99"]
        self.assertEqual(rec.sha256, sha256_bytes(paths[0].read_bytes()))
        self.assertEqual(rec.license, "CC BY 4.0 (Open-Meteo; ERA5 by ECMWF/Copernicus)")
        self.assertEqual(rec.source, "Open-Meteo ERA5 archive")
        self.assertNotIn("raw sha256", rec.notes)

    def test_requests_the_range_from_two_years_before_the_first(self):
        # The first period of 2005 is cut a month before it starts, and a
        # trailing twelve-month window ending there reaches back to 2003.
        self.snapshot()
        self.assertEqual(len(self.session.urls), 2)
        self.assertIn("start_date=2003-01-01&end_date=2005-12-31", self.session.urls[0])
        self.assertIn("latitude=30.1000&longitude=-91.1000", self.session.urls[0])
        self.assertIn("daily=precipitation_sum,temperature_2m_mean", self.session.urls[0])

    def test_the_first_periods_trailing_twelve_months_are_covered(self):
        # What the two-year lookback buys: with a one-month lag the first
        # quarter of the first contract year is built from the twelve months
        # ending in December of the year before, which start in January of the
        # year before that. Under a one-year lookback this column was NaN.
        self.session = RangeSession(daily_mm=2.0)
        paths = self.snapshot()
        era5 = open_meteo.sources(paths, ["open-meteo/era5/99"])["era5"]
        spec = F.FeatureSpec("precip_12m", "era5", "precip_mm", "trailing_sum", 12)
        unit = ("99001", 2005, 1)
        frame = F.build_frame([spec], {"era5": era5}, [unit], 4)
        value = frame.row(unit)[0]
        self.assertFalse(math.isnan(value), "the first period has no trailing year")
        # Twelve months of 2 mm a day, December 2003 through November 2004.
        days = sum(calendar.monthrange(y, m)[1]
                   for y, m in [(2003, 12)] + [(2004, m) for m in range(1, 12)])
        self.assertAlmostEqual(value, 2.0 * days, places=9)
        self.assertEqual(spec.cutoff(unit, 4), F.month_index(2004, 12))

    def test_resumes_without_refetching(self):
        self.session = RangeSession()
        self.snapshot()
        self.snapshot()
        self.assertEqual(len(self.session.urls), 2)  # pinned and complete: nothing
        more = {**self.centroids, "99005": (29.75, -92.25)}
        self.snapshot(more)
        self.assertEqual(len(self.session.urls), 3)  # only the new county
        ids = [json.loads(ln)["id"] for ln in self.paths()[0].read_text().splitlines()]
        self.assertEqual(ids, ["99001", "99003", "99005"])
        self.snapshot(more, refresh=True)
        self.assertEqual(len(self.session.urls), 6)

    def test_an_extract_that_no_longer_matches_its_pin_is_refused(self):
        # Re-pinning edited bytes would rewrite the provenance of every card
        # scored against the old ones, so it is an error — and `refresh` is
        # the only way to say "discard this and pull it again".
        self.session = RangeSession()
        paths = self.snapshot()
        pinned = self.manifest.records["open-meteo/era5/99"].sha256
        edited = paths[0].read_text().replace("12.0", "99.0")
        paths[0].write_text(edited)
        on_disk = sha256_bytes(paths[0].read_bytes())
        with self.assertRaises(ConnectorError) as ctx:
            self.snapshot()
        message = str(ctx.exception)
        self.assertIn(pinned, message)
        self.assertIn(on_disk, message)
        self.assertIn("refresh", message)
        self.assertEqual(self.manifest.records["open-meteo/era5/99"].sha256, pinned)
        self.assertEqual(paths[0].read_text(), edited)  # untouched by the refusal
        # With refresh the file is discarded and every region is fetched again.
        before = len(self.session.urls)
        self.snapshot(refresh=True)
        self.assertEqual(len(self.session.urls), before + len(self.centroids))
        self.assertEqual(self.manifest.records["open-meteo/era5/99"].sha256, pinned)
        self.assertEqual(sha256_bytes(paths[0].read_bytes()), pinned)

    def paths(self):
        return [self.dir / "open_meteo" / "99_era5_monthly.jsonl"]

    def test_an_interrupted_pull_resumes_where_it_stopped(self):
        # A partial extract with no manifest record: the county it holds is
        # kept, the missing one is fetched, and the whole file is then pinned.
        self.session = RangeSession()
        path = self.paths()[0]
        path.parent.mkdir()
        self.snapshot({"99001": (30.1, -91.1)})
        del self.manifest.records["open-meteo/era5/99"]
        self.session.urls.clear()
        self.snapshot()
        self.assertEqual(len(self.session.urls), 1)
        self.assertIn("open-meteo/era5/99", self.manifest.records)

    def test_a_429_is_slept_through_and_retried(self):
        self.session = FakeSession(self.payload, rate_limit_first=2)
        self.snapshot()
        self.assertEqual(self.sleeps, [10.0, 20.0])
        self.assertEqual(len(self.session.urls), 4)

    def test_a_non_429_error_is_not_retried(self):
        class Broken(FakeSession):
            def get(self, url):
                raise ConnectorError(f"HTTP 500 for {url}")

        with self.assertRaises(ConnectorError):
            self.snapshot(session=Broken(self.payload))
        self.assertEqual(self.sleeps, [])

    def test_keep_raw_writes_the_response_and_notes_its_checksum(self):
        self.snapshot(keep_raw=True)
        raw = self.dir / "open_meteo_raw" / "99001.json"
        self.assertTrue(raw.exists())
        self.assertNotIn("generationtime_ms", raw.read_text())
        notes = self.manifest.records["open-meteo/era5/99"].notes
        self.assertIn(f"99001:{sha256_bytes(raw.read_bytes())[:12]}", notes)

    def test_a_national_pull_is_one_extract_under_all(self):
        paths = self.snapshot({**self.centroids, "98001": (35.0, -97.0)}, scope="all")
        self.assertEqual(paths, [self.dir / "open_meteo" / "all_era5_monthly.jsonl"])
        self.assertIn("open-meteo/era5/all", self.manifest.records)

    def test_sources_serve_series_and_elevation(self):
        paths = self.snapshot()
        srcs = open_meteo.sources(paths, ["open-meteo/era5/99"])
        self.assertEqual(sorted(srcs), ["elevation", "era5"])
        era5, elevation = srcs["era5"], srcs["elevation"]
        self.assertEqual((era5.kind, era5.name), ("series", "era5"))
        self.assertEqual((elevation.kind, elevation.derived_through), ("static", None))
        s = era5.series("99001", "precip_mm")
        self.assertEqual(s.start, F.month_index(2004, 1))
        self.assertEqual(s.values[:2], (46.5, 14.5))
        self.assertTrue(math.isnan(s.values[14]))
        self.assertEqual(era5.series("99001", "tmean_c").values[0], 16.0)
        self.assertIsNone(era5.series("99001", "wind"))
        self.assertIsNone(era5.series("00000", "precip_mm"))
        self.assertEqual(elevation.static("99003"), {"elevation_m": 12.0})
        self.assertIsNone(elevation.static("00000"))
        contract = make_contract()
        F.admit(era5, contract)
        F.admit(elevation, contract)

    def test_extract_schema_drift_is_an_error(self):
        paths = self.snapshot()
        line = json.loads(paths[0].read_text().splitlines()[0])
        del line["tmean_c"]
        paths[0].write_text(json.dumps(line) + "\n")
        with self.assertRaises(ConnectorError):
            open_meteo.sources(paths, ["open-meteo/era5/99"])
        with self.assertRaises(ConnectorError):
            open_meteo.sources([self.dir / "nowhere.jsonl"], [])


# ---------------------------------------------------------------------------
# FEMA NRI
# ---------------------------------------------------------------------------


class TestNri(unittest.TestCase):
    sample = (DATA / "nri_sample.csv").read_bytes()

    def test_parse_the_committed_sample(self):
        table = nri.parse(self.sample)
        self.assertEqual(sorted(table), ["99001", "99003", "99005"])
        self.assertEqual(
            table["99001"],
            {
                "EAL_SCORE": 44.1,
                "RISK_SCORE": 45.6,
                "SOVI_SCORE": 60.2,
                "RESL_SCORE": 52.3,
            },
        )
        self.assertTrue(math.isnan(table["99003"]["SOVI_SCORE"]))  # blank cell

    def test_unpadded_fips_is_padded(self):
        unpadded = self.sample.replace(b",99001,", b",9001,").replace(b"C99001", b"C9001")
        table = nri.parse(unpadded)
        self.assertIn("09001", table)

    def test_schema_drift_is_an_error(self):
        with self.assertRaises(ConnectorError):
            nri.parse(self.sample.replace(b"EAL_SCORE", b"EAL_SCR"))

    def test_the_vintage_is_the_reviewed_constant(self):
        self.assertEqual(nri.VINTAGE.version, "1.20")
        self.assertEqual(nri.VINTAGE.derived_through, 2023)
        self.assertEqual(nri.MANIFEST_KEY, "fema/nri_counties_1.20")
        src = nri.source(nri.parse(self.sample))
        self.assertEqual((src.name, src.kind), ("nri", "static"))
        self.assertEqual(src.derived_through, 2023)
        self.assertEqual(src.manifest_keys, (nri.MANIFEST_KEY,))
        self.assertEqual(sorted(src.static("99005")), sorted(nri.COLUMNS))
        self.assertIsNone(src.static("00000"))

    def test_refused_under_the_default_splits_and_admitted_after_2023(self):
        src = nri.source(nri.parse(self.sample))
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(src, make_contract())  # validate starts 2016
        later = make_contract(
            splits={"train": [1996, 2023], "validate": [2024, 2025], "test": [2026, 2027]}
        )
        F.admit(src, later)

    def test_load_pins_the_vintage_into_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Manifest(path=pathlib.Path(tmp) / "manifest.json")
            with mock.patch.object(nri, "fetch", lambda url, **_kw: self.sample):
                table = nri.load(pathlib.Path(tmp), manifest)
            self.assertEqual(len(table), 3)
            rec = manifest.records[nri.MANIFEST_KEY]
            self.assertIn("derived_through=2023", rec.notes)
            self.assertEqual(rec.sha256, sha256_bytes(self.sample))
            self.assertIn("FEMA", rec.license)


# ---------------------------------------------------------------------------
# CLIMADA layer
# ---------------------------------------------------------------------------


class TestClimadaLayer(unittest.TestCase):
    sample = (DATA / "climada_sample.jsonl").read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.key = climada_layer.manifest_key("inland_flood", "US:ZZ")
        self.path = climada_layer.layer_path(self.dir, "inland_flood", "US:ZZ")

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout(self):
        self.assertEqual(self.key, "climada/inland_flood_US:ZZ")
        self.assertEqual(self.path, self.dir / "climada" / "inland_flood_US:ZZ.jsonl")

    def test_parse_the_committed_sample(self):
        layer = climada_layer.parse(self.sample)
        self.assertEqual(layer.header.event_set_years, (1980, 2015))
        self.assertEqual(layer.header.seed, 20260101)
        self.assertEqual(layer.derived_through, 2015)
        self.assertEqual(layer.rows["99001"], {"rp10": 0.8, "rp50": 1.6, "rp100": 2.1})

    def test_schema_errors(self):
        header, *rows = self.sample.decode().splitlines()
        bad = [
            "",
            "not json\n",
            "\n".join([json.dumps({"seed": 1}), *rows]),
            "\n".join([header, json.dumps({"region": "1", "rp10": 1.0, "rp50": 2.0})]),
            "\n".join(
                [header, json.dumps({"region": "1", "rp10": "x", "rp50": 2, "rp100": 3})]
            ),
            "\n".join([header.replace("[1980, 2015]", "[2015, 1980]"), *rows]),
            "\n".join([header.replace("20260101", '"abc"'), *rows]),
            header,
        ]
        for text in bad:
            with self.assertRaises(ConnectorError, msg=text):
                climada_layer.parse(text.encode())

    def test_load_pins_the_file_and_names_the_vintage(self):
        self.path.parent.mkdir()
        self.path.write_bytes(self.sample)
        layer = climada_layer.load(self.path, self.manifest, self.key)
        rec = self.manifest.records[self.key]
        self.assertEqual(rec.sha256, sha256_bytes(self.sample))
        self.assertEqual(rec.license, "GPL-3.0 tool output; layer values CC BY 4.0")
        self.assertEqual(rec.source, "CLIMADA event set")
        self.assertIn("derived_through=2015", rec.notes)
        self.manifest.save()
        self.assertEqual(climada_layer.load(self.path, self.manifest, self.key), layer)
        self.assertFalse(self.manifest.dirty)  # already pinned: untouched

    def test_an_absent_layer_names_the_tool_that_makes_it(self):
        with self.assertRaises(ConnectorError) as ctx:
            climada_layer.load(self.path, self.manifest, self.key)
        self.assertIn("tools/climada/run_event_set.py", str(ctx.exception))

    def test_source_derived_through_decides_admission(self):
        layer = climada_layer.parse(self.sample)
        src = climada_layer.source(layer, self.key)
        self.assertEqual((src.name, src.kind), ("climada", "static"))
        self.assertEqual(src.derived_through, 2015)
        self.assertEqual(src.manifest_keys, (self.key,))
        self.assertEqual(src.static("99003")["rp100"], 1.1)
        self.assertIsNone(src.static("00000"))
        F.admit(src, make_contract())  # validate starts 2016: through 2015 is fine
        through_2020 = self.sample.replace(b"[1980, 2015]", b"[1980, 2020]")
        recent = climada_layer.parse(through_2020)
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(climada_layer.source(recent, self.key), make_contract())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# FEMA / ORNL USA Structures
# ---------------------------------------------------------------------------


def _feature(fips, occ_cls, prim_occ, n) -> dict:
    return {"attributes": {"FIPS": fips, "OCC_CLS": occ_cls, "PRIM_OCC": prim_occ, "n": n}}


#: Two canned statistics pages for state 99, page size 3: the first is full and
#: says more follow, the second is short. Deliberately unsorted, one FIPS as an
#: integer, one pair the occupancy mapping does not know.
PAGES: list[dict] = [
    {
        "features": [
            _feature(99003, "Residential", "Single Family Dwelling", 120),
            _feature("99001", "Education", "Grade Schools", 4),
            _feature("99001", "Residential", "Single Family Dwelling", 300),
        ],
        "exceededTransferLimit": True,
    },
    {
        "features": [
            _feature("99001", "Commercial", "Hospital", 2),
            _feature("99003", "Weird", "Spaceport", 5),
        ],
        "exceededTransferLimit": False,
    },
]
LAST_EDIT_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
METADATA = {"editingInfo": {"lastEditDate": LAST_EDIT_MS}}


class FakeLayer:
    """An ArcGIS layer answering the metadata call and paged statistics queries."""

    def __init__(self, pages: list[dict], metadata: dict | None = METADATA, page: int = 3):
        self.pages = pages
        self.metadata = metadata
        self.page = page
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if "/query?" not in url:
            if self.metadata is None:
                raise ConnectorError(f"HTTP 404 for {url}")
            return json.dumps(self.metadata).encode()
        params = dict(
            pair.split("=", 1) for pair in url.split("?", 1)[1].split("&")
        )
        offset = int(params["resultOffset"])
        index = offset // self.page
        if index >= len(self.pages):
            return json.dumps({"features": [], "exceededTransferLimit": False}).encode()
        return json.dumps(self.pages[index]).encode()


class CappedLayer:
    """A layer whose `maxRecordCount` is below the page the caller asks for.

    ArcGIS answers `resultRecordCount=5` with two rows and sets
    `exceededTransferLimit`, because the cap is the layer's, not the caller's.
    A client that waits for a page as full as it asked for stops after the
    first page and pins a fraction of a state as if it were the whole of it.
    """

    def __init__(self, groups: list[dict], cap: int = 2, metadata: dict | None = METADATA):
        self.groups = list(groups)
        self.cap = cap
        self.metadata = metadata
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if "/query?" not in url:
            return json.dumps(self.metadata).encode()
        params = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
        offset = int(params["resultOffset"])
        page = self.groups[offset:offset + min(int(params["resultRecordCount"]), self.cap)]
        return json.dumps({
            "features": page,
            "exceededTransferLimit": offset + len(page) < len(self.groups),
        }).encode()


class TestUsaStructures(unittest.TestCase):
    sample = (DATA / "usa_structures_sample.jsonl").read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.layer = FakeLayer(PAGES)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, states=("99",), **kw):
        kw.setdefault("session", self.layer)
        kw.setdefault("page", 3)
        return usa_structures.snapshot(list(states), self.dir, self.manifest, **kw)

    def test_counts_query_shape(self):
        url = usa_structures.counts_query("22", 40, 20)
        self.assertTrue(url.startswith(usa_structures.LAYER_URL + "/query?"))
        query = url.split("?", 1)[1]
        self.assertIn("where=FIPS+LIKE+%2722%25%27", query)
        self.assertIn("groupByFieldsForStatistics=FIPS%2COCC_CLS%2CPRIM_OCC", query)
        self.assertIn("resultOffset=40", query)
        self.assertIn("resultRecordCount=20", query)
        self.assertIn("returnGeometry=false", query)  # counts only; no footprint
        self.assertIn("f=json", query)
        stats = "%5B%7B%22statisticType%22%3A%22count%22%2C%22onStatisticField%22%3A%22OBJECTID"
        self.assertIn(stats, query)
        self.assertIn("outStatisticFieldName%22%3A%22n%22", query)
        other = usa_structures.counts_query("22", layer_url="https://mirror.invalid/L/0/")
        self.assertTrue(other.startswith("https://mirror.invalid/L/0/query?"))
        with self.assertRaises(ConnectorError):
            usa_structures.counts_query("LA")

    def test_paged_stats_hashed_together(self):
        paths = self.snapshot()
        self.assertEqual(paths, [self.dir / "usa_structures" / "99_counts.jsonl"])
        # The metadata call, then two pages: the second was short, so no third.
        queries = [u for u in self.layer.urls if "/query?" in u]
        self.assertEqual(len(self.layer.urls), 3)
        self.assertEqual(len(queries), 2)
        self.assertIn("resultOffset=0&", queries[0])
        self.assertIn("resultOffset=3&", queries[1])
        # The extract is the sorted union of both pages, exactly as committed.
        self.assertEqual(paths[0].read_bytes(), self.sample)
        rec = self.manifest.records["fema/usa_structures/99"]
        self.assertEqual(rec.sha256, sha256_bytes(self.sample))
        self.assertEqual(rec.bytes, len(self.sample))
        self.assertEqual(rec.license, usa_structures.LICENSE)
        self.assertEqual(rec.source, usa_structures.SOURCE)
        self.assertIn("2 page(s)", rec.notes)
        self.assertIn("2 counties, 5 groups", rec.notes)

    def test_a_layer_cap_below_the_requested_page_still_pages_to_the_end(self):
        # Seven groups, a layer that will not serve more than two at a time,
        # and a caller asking for five: every page is short and every page but
        # the last says more follow, so only the server's signal may stop it.
        groups = [
            _feature(f"9900{i}", "Residential", "Single Family Dwelling", i)
            for i in range(1, 8)
        ]
        rows, pages = usa_structures.fetch_counts(
            "99", session=CappedLayer(groups, cap=2), page=5
        )
        self.assertEqual([r["fips"] for r in rows], [f"9900{i}" for i in range(1, 8)])
        self.assertEqual(pages, 4)  # 2 + 2 + 2 + 1
        paths = self.snapshot(session=CappedLayer(groups, cap=2), page=5)
        self.assertEqual(len(paths[0].read_text().splitlines()), 7)
        notes = self.manifest.records["fema/usa_structures/99"].notes
        self.assertIn("4 page(s)", notes)
        self.assertIn("7 counties, 7 groups", notes)

    def test_a_fips_finer_than_a_county_is_refused_not_truncated(self):
        # `int()` on a ten-digit tract id drops its leading zero and yields
        # something that is neither a tract nor a county. Nothing below the
        # county may enter the extract, so the page is refused instead.
        for raw in ("0100102010", "01001020100", "010010201001"):
            with self.subTest(fips=raw), self.assertRaises(ConnectorError) as ctx:
                usa_structures.parse_page(
                    {"features": [_feature(raw, "Residential", "Dwelling", 3)]}
                )
            self.assertIn("county code is", str(ctx.exception))
        # A short numeric code is still zero-padded to its county.
        rows = usa_structures.parse_page(
            {"features": [_feature(1001, "Residential", "Dwelling", 3)]}
        )
        self.assertEqual(rows[0]["fips"], "01001")

    def test_vintage_recorded(self):
        self.snapshot()
        rec = self.manifest.records["fema/usa_structures/99"]
        self.assertIn("derived_through=2023", rec.notes)
        self.assertIn("lastEditDate 2023-11-14", rec.notes)
        self.assertIn(f"layer {usa_structures.LAYER_URL}", rec.notes)
        self.assertEqual(usa_structures.vintage_of(rec), 2023)
        self.assertTrue(rec.url.startswith(usa_structures.LAYER_URL + "/query?"))

    def test_layer_url_override_is_resolved_into_the_record(self):
        url = "https://mirror.invalid/USA_Structures/FeatureServer/0"
        self.snapshot(layer_url=url)
        rec = self.manifest.records["fema/usa_structures/99"]
        self.assertIn(f"layer {url}", rec.notes)
        self.assertTrue(all(u.startswith(url) for u in self.layer.urls))

    def test_vintage_falls_back_to_the_snapshot_year_when_metadata_is_unavailable(self):
        self.snapshot(session=FakeLayer(PAGES, metadata=None))
        rec = self.manifest.records["fema/usa_structures/99"]
        year = int(utc_now()[:4])
        self.assertIn(f"derived_through={year}", rec.notes)
        self.assertIn("unavailable", rec.notes)
        self.snapshot(session=FakeLayer(PAGES, metadata={"editingInfo": {}}), refresh=True)
        self.assertIn("unavailable", self.manifest.records["fema/usa_structures/99"].notes)

    def test_a_pinned_extract_is_not_refetched(self):
        self.snapshot()
        self.snapshot()
        self.assertEqual(len(self.layer.urls), 3)
        self.snapshot(refresh=True)
        self.assertEqual(len(self.layer.urls), 6)

    def test_a_tampered_extract_is_refetched(self):
        paths = self.snapshot()
        paths[0].write_text("{}\n")
        self.snapshot()
        self.assertEqual(len(self.layer.urls), 6)
        self.assertEqual(paths[0].read_bytes(), self.sample)

    def test_schema_drift_raises(self):
        drifted = [
            {"features": [{"attributes": {"FIPS": "99001", "OCC_CLS": "x", "n": 1}}]},
        ]
        with self.assertRaises(ConnectorError) as ctx:
            self.snapshot(session=FakeLayer(drifted))
        self.assertIn("PRIM_OCC", str(ctx.exception))
        with self.assertRaises(ConnectorError):
            self.snapshot(session=FakeLayer([{"rows": []}]))
        with self.assertRaises(ConnectorError):
            self.snapshot(session=FakeLayer([{"error": {"code": 400}}]))
        with self.assertRaises(ConnectorError):
            self.snapshot(session=FakeLayer([{"features": []}]))
        self.assertNotIn("fema/usa_structures/99", self.manifest.records)

    def test_read_extract_checks_every_line(self):
        rows = usa_structures.read_extract(self.sample)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], {"fips": "99001", "occ_cls": "Commercial",
                                   "prim_occ": "Hospital", "n": 2})
        with self.assertRaises(ConnectorError):
            usa_structures.read_extract(b'{"fips":"99001","occ_cls":"a","n":1}\n')
        with self.assertRaises(ConnectorError):
            usa_structures.read_extract(b'{"fips":"99001","occ_cls":"a","prim_occ":"b","n":-1}\n')
        with self.assertRaises(ConnectorError):
            usa_structures.read_extract(b"not json\n")

    def test_refused_as_feature_by_admit(self):
        from readiness.exposure.table import ExposureTable

        self.snapshot()
        table = ExposureTable.load(self.dir, self.manifest, ["99"])
        src = usa_structures.source(table)
        self.assertEqual((src.name, src.kind), ("usa_structures", "static"))
        self.assertEqual(src.derived_through, 2023)
        self.assertEqual(src.manifest_keys, ("fema/usa_structures/99",))
        self.assertFalse(src.global_coverage)
        self.assertEqual(src.static("99001")["total"], 306.0)
        self.assertEqual(src.static("99001")["hospital"], 2.0)
        self.assertIsNone(src.static("00000"))
        self.assertIsNone(src.series("99001", "total"))
        self.assertIsInstance(src, F.FeatureSource)
        # Exposure is a join, never a covariate: every current contract
        # validates from 2016, and the layer is maintained through 2023.
        with self.assertRaises(F.FeatureAdmissionError) as ctx:
            F.admit(src, make_contract())
        self.assertIn("usa_structures", str(ctx.exception))
        for path in sorted((REPO_ROOT / "contracts").glob("*.json")):
            with self.subTest(contract=path.name), self.assertRaises(F.FeatureAdmissionError):
                F.admit(src, Contract.from_path(path))


class TestRefreshWithoutFetching(unittest.TestCase):
    """`refresh=True` means download; with fetching disallowed that is an error."""

    def test_census_refuses(self):
        from readiness.connectors import census
        from readiness.connectors.base import ConnectorError, Manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(ConnectorError):
                census.load(root, Manifest(path=root / "m.json"), refresh=True, allow_fetch=False)

    def test_nws_zones_refuses(self):
        from readiness.connectors import nws_zones
        from readiness.connectors.base import ConnectorError, Manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(ConnectorError):
                nws_zones.load(root, Manifest(path=root / "m.json"), refresh=True, allow_fetch=False)
