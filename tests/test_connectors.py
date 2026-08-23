"""Provenance and transport.

These tests cover the two bugs that made it into a working build and had to be
fixed: a manifest that claimed provenance it did not have, and a connect path
that took six minutes to reach a host curl reached in 0.6 seconds.
"""

import pathlib
import socket
import tempfile
import unittest

from readiness.connectors import storm_events
from readiness.connectors.base import (
    CONNECT_TIMEOUT,
    Manifest,
    SourceRecord,
    _connect_socket,
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


class TestUnpinnedCacheIsRefetched(unittest.TestCase):
    """A cached extract with no manifest record must not be trusted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "storm_events").mkdir(parents=True)
        # Simulate the state left by a download that completed but crashed
        # before the manifest was saved: bytes on disk, no provenance.
        for year in (2000, 2001):
            (self.dir / "storm_events" / f"22_{year}.jsonl").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_without_manifest_record_is_treated_as_missing(self):
        manifest = Manifest(path=self.dir / "manifest.json")
        calls: list[int] = []

        def fake_discover(years):
            calls.extend(years)
            raise storm_events.ConnectorError("stop here; we only need the years")

        original = storm_events.discover_files
        storm_events.discover_files = fake_discover
        try:
            with self.assertRaises(storm_events.ConnectorError):
                storm_events.snapshot([2000, 2001], "22", self.dir, manifest)
        finally:
            storm_events.discover_files = original

        self.assertEqual(sorted(calls), [2000, 2001])

    def test_extract_with_manifest_record_is_reused(self):
        manifest = Manifest(path=self.dir / "manifest.json")
        for year in (2000, 2001):
            manifest.add(f"noaa/storm_events/{year}", record())

        def fail(_years):
            raise AssertionError("should not re-download a pinned extract")

        original = storm_events.discover_files
        storm_events.discover_files = fail
        try:
            out = storm_events.snapshot([2000, 2001], "22", self.dir, manifest)
        finally:
            storm_events.discover_files = original
        self.assertTrue(out.exists())


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
