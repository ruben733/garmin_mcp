"""Offline security/regression checks; only synthetic token data is used."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location(
    "persistence", Path(__file__).parents[2] / "src/garmin_mcp/token_persistence.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
DATA = '{"di_token":"synthetic-only","di_refresh_token":"synthetic-refresh","di_client_id":"synthetic-client"}'

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "tokens"

    def configure(self):
        os.environ.update(UPSTASH_REDIS_REST_URL="https://example.upstash.io",
                          UPSTASH_REDIS_REST_TOKEN="synthetic-secret")

    def test_disabled(self):
        self.assertFalse(p.configured())
        self.assertFalse(p.restore_tokens(self.directory))

    def test_incomplete_or_insecure_configuration(self):
        for url in ["http://example.com", "https://user:pass@example.com",
                    "https://example.com?token=x", ""]:
            with patch.dict(os.environ, {"UPSTASH_REDIS_REST_URL":url,
                                        "UPSTASH_REDIS_REST_TOKEN":"synthetic"}):
                with self.assertRaises(p.PersistenceError):
                    p.configured()

    def test_post_body_not_url(self):
        self.configure()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"result":1}'
        with patch.object(p.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertTrue(p.backup_tokens(self.directory, DATA, expected=DATA))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.upstash.io")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.data), ["EVAL", p.CAS_SCRIPT, "1", p.REDIS_KEY, DATA, DATA])

    def test_atomic_restore_and_permissions(self):
        with patch.object(p, "_upstash_request", return_value={"result":DATA}):
            self.assertTrue(p.restore_tokens(self.directory))
        target = self.directory / "garmin_tokens.json"
        self.assertEqual(target.read_text(), DATA)
        self.assertEqual(list(self.directory.iterdir()), [target])
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_invalid_remote_preserves_local(self):
        p.write_tokens(self.directory, DATA)
        for invalid in ["{}", "[]", "broken", "", 123]:
            with patch.object(p, "_upstash_request", return_value={"result":invalid}):
                with self.assertRaises(p.PersistenceError):
                    p.restore_tokens(self.directory)
            self.assertEqual(p.read_file(self.directory / "garmin_tokens.json"), DATA)

    def test_outage_does_not_fallback_or_overwrite(self):
        self.configure()
        with patch.object(p, "restore_tokens", side_effect=p.PersistenceError), patch.object(p, "write_tokens") as write:
            with self.assertRaises(p.PersistenceError):
                p.bootstrap(self.directory)
            write.assert_not_called()

    def test_seed_and_local_precedence(self):
        seed = Path(self.tmp.name) / "seed.json"
        seed.write_text(DATA)
        os.environ["GARMIN_TOKEN_SEED"] = str(seed)
        self.assertFalse(p.bootstrap(self.directory))
        seed.write_text('{"access_token":"older"}')
        p.bootstrap(self.directory)
        self.assertEqual(p.read_file(self.directory / "garmin_tokens.json"), DATA)

    def test_remote_precedes_seed(self):
        self.configure()
        with patch.object(p, "_upstash_request", return_value={"result":DATA}):
            self.assertTrue(p.bootstrap(self.directory))
        self.assertEqual(p.read_file(self.directory / "garmin_tokens.json"), DATA)

    def test_errors_are_sanitized(self):
        self.configure()
        with patch.object(p.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = OSError("synthetic-secret")
            with self.assertRaises(p.PersistenceError) as caught:
                p._upstash_request(["GET", p.REDIS_KEY])
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_redirects_rejected(self):
        self.assertIsNone(p.NoRedirect().redirect_request(None,None,302,None,None,"https://elsewhere"))

    def test_failed_write(self):
        with patch.object(p, "_upstash_request", return_value={"result":"unexpected"}):
            with self.assertRaises(p.PersistenceError):
                p.backup_tokens(self.directory, DATA, expected=DATA)

    def test_partial_and_oversized_files_not_uploaded(self):
        self.directory.mkdir()
        target = self.directory / "garmin_tokens.json"
        for data in ['{"access_token":', "x" * (p.MAX_BYTES + 1)]:
            target.write_text(data)
            with patch.object(p, "_upstash_request") as request:
                with self.assertRaises(p.PersistenceError):
                    p.backup_tokens(self.directory, expected=DATA)
                request.assert_not_called()

    def test_empty_remote_rejects_obsolete_seed_and_local(self):
        self.configure()
        seed = Path(self.tmp.name) / "old.json"
        seed.write_text(DATA)
        os.environ["GARMIN_TOKEN_SEED"] = str(seed)
        for local in (False, True):
            if local:
                p.write_tokens(self.directory, DATA)
            with patch.object(p, "_upstash_request", return_value={"result":None}) as request:
                with self.assertRaises(p.PersistenceError):
                    p.bootstrap(self.directory)
                self.assertEqual(request.call_args_list[0].args[0][0], "GET")
                self.assertEqual(request.call_count, 1)

    def test_explicit_initialization_never_overwrites(self):
        self.configure()
        source = Path(self.tmp.name) / "current.json"
        source.write_text(DATA)
        with patch.object(p, "_upstash_request", return_value={"result":"OK"}) as request:
            p.initialize_remote(source)
            self.assertEqual(request.call_args.args[0], ["SET", p.REDIS_KEY, DATA, "NX"])
        with patch.object(p, "_upstash_request", return_value={"result":None}):
            with self.assertRaises(p.PersistenceError):
                p.initialize_remote(source)

    def test_concurrent_writer_conflict(self):
        with patch.object(p, "_upstash_request", return_value={"result":0}):
            with self.assertRaises(p.PersistenceConflict):
                p.backup_tokens(self.directory, DATA, expected=DATA)

    def test_incomplete_token_objects_rejected(self):
        for data in ('{"unrelated":"value"}', '{"di_token":"x"}',
                     '{"di_token":"x","di_refresh_token":"r","di_client_id":null}'):
            with self.assertRaises(p.PersistenceError):
                p.valid_payload(data)

    def run_supervisor(self, child, backup):
        os.environ["GARMINTOKENS"] = str(self.directory)
        self.configure()
        p.write_tokens(self.directory, DATA)
        with patch.object(p, "bootstrap", return_value=True), \
             patch.object(p.subprocess, "Popen", return_value=child), \
             patch.object(p.signal, "signal"), \
             patch.object(p, "backup_tokens", backup), \
             patch.object(p.os, "umask"):
            return p.main()

    def test_unchanged_restore_is_not_uploaded(self):
        child = MagicMock()
        child.poll.return_value = 0
        child.returncode = 0
        backup = MagicMock()
        self.assertEqual(self.run_supervisor(child, backup), 0)
        backup.assert_not_called()

    def test_shutdown_flush_uses_original_remote_version(self):
        updated = DATA.replace("synthetic-only", "synthetic-refreshed")
        child = MagicMock()
        def exited():
            p.write_tokens(self.directory, updated)
            return 0
        child.poll.side_effect = exited
        child.returncode = 0
        backup = MagicMock(return_value=True)
        self.assertEqual(self.run_supervisor(child, backup), 0)
        backup.assert_called_once_with(str(self.directory), updated, expected=DATA)

if __name__ == "__main__":
    unittest.main()
