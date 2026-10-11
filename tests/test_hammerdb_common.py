import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hammerdb_common import (
    HAMMERDB_TARBALL_SHA256,
    database_port,
    engine_id_from_label,
    measurement_interval,
    parse_cli,
    parse_native_output,
    port_is_available,
    required_transaction_log_bytes,
    storage_root,
)


def native_output(backend="MariaDB", workload_vus=2, nopm="1234.50", tpm="2345.60"):
    workers = "\n".join(
        "Vuser %d:FINISHED SUCCESS" % vuser_id
        for vuser_id in range(2, workload_vus + 2)
    )
    return (
        "Vuser 1:Beginning rampup time of 0 minutes\n"
        "Vuser 1:Rampup complete, Taking start Transaction Count.\n"
        "Vuser 1:Timing test period of 1 in minutes\n"
        "Vuser 1:Test complete, Taking end Transaction Count.\n"
        "Vuser 1:TEST RESULT : System achieved %s NOPM from %s %s TPM\n"
        "%s\n"
        "Vuser 1:FINISHED SUCCESS\n"
        "ALL VIRTUAL USERS COMPLETE\n"
        % (nopm, tpm, backend, workers)
    )


class HammerDBCommonTests(unittest.TestCase):
    def test_cli_fills_defaults_and_rejects_unbounded_values(self):
        args = parse_cli(["--backend", "postgresql", "--virtual-users", "4"])
        self.assertEqual(args.backend, "postgresql")
        self.assertEqual(args.warehouses, 1)
        self.assertEqual(args.rampup_minutes, 1)
        self.assertEqual(args.duration_minutes, 1)
        with self.assertRaises(ValueError):
            parse_cli(["--warehouses", "5"])
        with self.assertRaises(ValueError):
            parse_cli(["--duration-minutes", "0"])
        with self.assertRaises(ValueError):
            parse_cli(["--unknown-option", "1"])

    def test_engine_ports_are_deterministic_and_distinct_by_backend(self):
        self.assertEqual(engine_id_from_label("client-3"), 3)
        self.assertEqual(database_port(3, "mariadb"), 40004)
        self.assertEqual(database_port(3, "postgresql"), 40005)
        with self.assertRaises(ValueError):
            engine_id_from_label("server-3")
        with self.assertRaises(ValueError):
            engine_id_from_label("client-10500")

    def test_port_probe_rejects_live_listener(self):
        for bind_address in ("127.0.0.1", "0.0.0.0"):
            for reuse_address in (0, 1):
                with self.subTest(bind_address=bind_address, reuse_address=reuse_address):
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse_address)
                        listener.bind((bind_address, 0))
                        listener.listen(1)
                        self.assertFalse(port_is_available(listener.getsockname()[1]))

    def test_port_probe_accepts_closed_listener_in_time_wait(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            port = listener.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(("127.0.0.1", port))
                connection, _ = listener.accept()
                connection.close()  # Server closes first, leaving its port in TIME_WAIT.
                self.assertEqual(client.recv(1), b"")
                client.shutdown(socket.SHUT_WR)

        def is_time_wait():
            try:
                rows = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                return False
            return any(
                int(row.split()[1].rsplit(":", 1)[1], 16) == port
                and row.split()[3] == "06"
                for row in rows
            )

        deadline = time.monotonic() + 2
        while not is_time_wait() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not is_time_wait():
            self.skipTest("loopback close did not leave a visible TCP TIME_WAIT entry")
        self.assertTrue(port_is_available(port))

    def test_pinned_archive_hashes_are_sha256_length(self):
        self.assertEqual(set(HAMMERDB_TARBALL_SHA256), {"x86_64", "aarch64"})
        for digest in HAMMERDB_TARBALL_SHA256.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_native_result_is_validated_for_both_backends(self):
        mariadb = parse_native_output(native_output(), "mariadb", 1, 2, 0, 1)
        postgres = parse_native_output(native_output("PostgreSQL"), "postgresql", 1, 2, 0, 1)
        self.assertEqual(mariadb["nopm"], 1234.5)
        self.assertEqual(mariadb["tpm_backend"], "MariaDB")
        self.assertEqual(postgres["tpm_backend"], "PostgreSQL")
        self.assertEqual(postgres["monitor_virtual_users"], 1)
        self.assertEqual(postgres["total_virtual_users"], 3)

    def test_native_result_rejects_backend_mismatch_and_bad_vus(self):
        with self.assertRaisesRegex(ValueError, "different database backend"):
            parse_native_output(native_output(), "postgresql", 1, 2, 0, 1)
        early_worker = native_output().replace(
            "Vuser 1:Test complete, Taking end Transaction Count.",
            "Vuser 2:FINISHED SUCCESS\nVuser 1:Test complete, Taking end Transaction Count.",
        )
        with self.assertRaisesRegex(ValueError, "before the timed window ended"):
            parse_native_output(early_worker, "mariadb", 1, 2, 0, 1)
        failed_worker = native_output().replace("Vuser 2:FINISHED SUCCESS", "Vuser 2:FINISHED FAILED")
        with self.assertRaisesRegex(ValueError, "reported failure"):
            parse_native_output(failed_worker, "mariadb", 1, 2, 0, 1)

    def test_native_result_rejects_duplicate_or_zero_scores(self):
        output = native_output()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_native_output(output + output, "mariadb", 1, 2, 0, 1)
        with self.assertRaisesRegex(ValueError, "finite positive"):
            parse_native_output(native_output(nopm="0"), "mariadb", 1, 2, 0, 1)

    def test_measurement_window_uses_monotonic_elapsed_time(self):
        wall_start = 1_700_000_000_123_000_000
        wall_end = wall_start + 60_100_000_000
        mono_start = 900_000_000_000
        mono_end = mono_start + 60_050_000_000
        result = measurement_interval(wall_start, wall_end, 1, mono_start, mono_end)
        self.assertEqual(result["begin_ms"], wall_start // 1_000_000)
        self.assertEqual(result["elapsed_seconds"], 60.05)
        self.assertIn("receipt", result["timestamp_source"])
        with self.assertRaisesRegex(ValueError, "differs from configured"):
            measurement_interval(wall_start, wall_start + 30_000_000_000, 1, mono_start, mono_start + 30_000_000_000)

    def test_storage_root_resolution_and_explicit_mount_requirements(self):
        self.assertEqual(storage_root(".", cwd="/tmp"), __import__("pathlib").Path("/tmp"))
        with self.assertRaisesRegex(ValueError, "absolute"):
            storage_root("relative", cwd="/tmp")
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            storage_root("/", cwd="/tmp")
        with tempfile.TemporaryDirectory() as temporary_directory:
            candidate = Path(temporary_directory) / "data"
            candidate.mkdir()
            with mock.patch("hammerdb_common.mount_provenance", return_value={"exact_mountpoint": False}):
                with self.assertRaisesRegex(ValueError, "engine mountpoint"):
                    storage_root(str(candidate), cwd="/tmp")
            with self.assertRaisesRegex(ValueError, "ASCII letters"):
                storage_root(str(candidate).replace("/", "/ ", 1), cwd="/tmp")

    def test_transaction_log_capacity_estimate_is_backend_specific(self):
        mariadb = parse_cli(["--backend=mariadb"])
        postgresql = parse_cli(["--backend=postgresql"])
        self.assertEqual(required_transaction_log_bytes(mariadb), 128 * 1024 * 1024)
        self.assertEqual(required_transaction_log_bytes(postgresql), 2 * 1024 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
