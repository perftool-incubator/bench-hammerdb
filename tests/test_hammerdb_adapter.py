import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hammerdb_client_adapter", PROJECT_ROOT / "hammerdb-client.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class HammerDBAdapterTests(unittest.TestCase):
    def test_legacy_mariadb_tools_are_selected_when_modern_names_are_missing(self):
        paths = {"mysql_install_db": "/usr/bin/mysql_install_db", "mysqld": "/usr/sbin/mysqld"}
        with mock.patch.object(adapter.shutil, "which", side_effect=lambda name: paths.get(name)):
            installer, installer_name = adapter.require_executable_any(
                "mariadb-install-db", "mysql_install_db"
            )
            daemon, daemon_name = adapter.require_executable_any("mariadbd", "mysqld")

        self.assertEqual(installer_name, "mysql_install_db")
        self.assertEqual(daemon_name, "mysqld")
        command = adapter.mariadb_install_command(installer, installer_name, "/private/server.cnf", "/private/data")
        self.assertNotIn("--auth-root-authentication-method=normal", command)
        self.assertIn("--skip-test-db", command)

    def test_modern_mariadb_tools_keep_explicit_root_authentication(self):
        command = adapter.mariadb_install_command(
            Path("/usr/bin/mariadb-install-db"),
            "mariadb-install-db",
            "/private/server.cnf",
            "/private/data",
        )
        self.assertIn("--auth-root-authentication-method=normal", command)

    def test_mariadb_server_is_found_in_libexec(self):
        with mock.patch.object(adapter.shutil, "which", return_value=None):
            with mock.patch.object(Path, "is_file", return_value=True):
                with mock.patch.object(adapter.os, "access", return_value=True):
                    executable, name = adapter.require_executable_any("mariadbd", "mysqld")

        self.assertEqual(executable, Path("/usr/libexec/mariadbd").resolve())
        self.assertEqual(name, "mariadbd")

    def test_generated_tcl_braces_values_and_rejects_tcl_structure(self):
        args = type("Args", (), {"warehouses": 1, "virtual_users": 2, "rampup_minutes": 1, "duration_minutes": 1})()
        parameters = {
            "connection": {"maria_host": "127.0.0.1", "maria_socket": "null"},
            "tpcc": {"maria_user": "hdb_0123", "maria_pass": "abc123"},
        }
        script = adapter.hammerdb_script("mariadb", parameters, "driver-check", args)
        self.assertIn("dbset db maria", script)
        self.assertIn("diset connection maria_socket {null}", script)
        self.assertIn("diset tpcc maria_pass {abc123}", script)
        with self.assertRaisesRegex(ValueError, "unsupported brace"):
            adapter.tcl_braced("bad}value")

    def test_driver_check_requires_the_embedded_native_package(self):
        args = type("Args", (), {"warehouses": 1, "virtual_users": 2, "rampup_minutes": 1, "duration_minutes": 1})()
        for backend, package in (("mariadb", "mariatcl"), ("postgresql", "Pgtcl")):
            script = adapter.hammerdb_script(backend, {}, "driver-check", args)
            self.assertIn("package require " + package, script)
            self.assertIn("HAMMERDB_NATIVE_DRIVER_PACKAGE " + package, script)

    def test_schema_success_markers_are_required(self):
        maria_build = "Vuser 1:TPCC SCHEMA COMPLETE\nVuser 1:FINISHED SUCCESS\n"
        pg_build = "Vuser 1:HDB_012345 SCHEMA COMPLETE\nVuser 1:FINISHED SUCCESS\n"
        check = (
            "Vuser 1:tpcc TPROC-C Schema has been checked successfully.\n"
            "Vuser 1:FINISHED SUCCESS\n"
        )
        adapter.validate_schema_output("schema-build", maria_build, "mariadb")
        adapter.validate_schema_output("schema-build", pg_build, "postgresql")
        adapter.validate_schema_output("schema-check", check, "mariadb")
        adapter.validate_schema_output("schema-check", check, "postgresql")
        with self.assertRaisesRegex(RuntimeError, "single schema VU"):
            adapter.validate_schema_output("schema-build", "HAMMERDB_SCHEMA_BUILD_COMPLETED\n", "mariadb")
        with self.assertRaisesRegex(RuntimeError, "schema-build marker"):
            adapter.validate_schema_output("schema-build", maria_build, "postgresql")
        with self.assertRaisesRegex(RuntimeError, "single schema VU"):
            adapter.validate_schema_output("schema-check", "HAMMERDB_SCHEMA_CHECK_COMPLETED\n", "postgresql")
        with self.assertRaisesRegex(RuntimeError, "schema checks"):
            adapter.validate_schema_output(
                "schema-check",
                "tpcc TPROC-C Schema has been checked successfully.\nVuser 1:FINISHED SUCCESS\n",
                "mariadb",
            )

    def test_process_group_kill_reaps_ignoring_child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.05)
            adapter.terminate_process_group(process, grace_seconds=0.05)
            self.assertEqual(process.returncode, -signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_database_cleanup_stops_descendant_after_leader_exited(self):
        code = (
            "import os,signal,time; "
            "child=os.fork(); "
            "(os._exit(0) if child else None); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.assertEqual(process.wait(timeout=2), 0)
        database = object.__new__(adapter.LocalDatabase)
        database.process = process
        database.log_stream = None
        adapter._register_process(process)
        with mock.patch.object(adapter, "DATABASE_SHUTDOWN_SECONDS", 0.05):
            result = database.cleanup()
        self.assertTrue(result["group_stopped"])
        self.assertTrue(adapter.process_group_is_stopped(process.pid))

    def test_database_liveness_uses_owned_child_when_proc_access_is_denied(self):
        class OwnedProcess:
            pid = os.getpid()
            args = [
                "/usr/sbin/mariadbd",
                "--datadir=/tmp/hammerdb-private/database",
                "--port=40004",
            ]

            def poll(self):
                return None

        with mock.patch.object(adapter.Path, "read_bytes", side_effect=PermissionError):
            marker = adapter.process_is_our_database(
                OwnedProcess(),
                "mariadb",
                "/tmp/hammerdb-private/database",
                40004,
            )

        self.assertTrue(marker["alive"])
        self.assertEqual(marker["command_line"], " ".join(OwnedProcess.args))
        self.assertEqual(marker["executable"], "/usr/sbin/mariadbd")
        self.assertIn("PermissionError", marker["proc_inspection_error"])

    def test_legacy_mysqld_name_is_accepted_for_mariadb(self):
        class LegacyMariaDBProcess:
            pid = os.getpid()
            args = [
                "/usr/libexec/mysqld",
                "--datadir=/tmp/hammerdb-private/database",
                "--port=40004",
            ]

            def poll(self):
                return None

        with mock.patch.object(adapter.Path, "read_bytes", side_effect=PermissionError):
            marker = adapter.process_is_our_database(
                LegacyMariaDBProcess(),
                "mariadb",
                "/tmp/hammerdb-private/database",
                40004,
            )

        self.assertTrue(marker["alive"])
        self.assertEqual(marker["executable"], "/usr/libexec/mysqld")

    def test_exited_database_process_records_return_code(self):
        class ExitedProcess:
            pid = os.getpid()
            args = [
                "/usr/sbin/mariadbd",
                "--datadir=/tmp/hammerdb-private/database",
                "--port=40004",
            ]

            def poll(self):
                return 17

        with mock.patch.object(adapter.Path, "read_bytes", side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, "return code 17"):
                adapter.process_is_our_database(
                    ExitedProcess(),
                    "mariadb",
                    "/tmp/hammerdb-private/database",
                    40004,
                )

    def test_closed_stdout_does_not_end_cli_supervision_early(self):
        args = type("Args", (), {"warehouses": 1, "virtual_users": 2, "rampup_minutes": 1, "duration_minutes": 1})()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_cli = root / "fake-hammerdb-cli"
            fake_cli.write_text(
                "#!%s\nimport os, time\nos.close(1)\ntime.sleep(0.3)\n" % sys.executable,
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            database = type(
                "Database",
                (),
                {
                    "backend": "mariadb",
                    "hammerdb_parameters": lambda self: {},
                    "hammerdb_environment": lambda self: os.environ.copy(),
                },
            )()
            started = time.monotonic()
            with mock.patch.object(adapter, "HAMMERDB_CLI", fake_cli):
                with mock.patch.object(adapter, "HAMMERDB_HOME", root):
                    with self.assertRaisesRegex(RuntimeError, "did not report its completion marker"):
                        adapter.run_hammerdb(args, database, root, (), "driver-check")
            self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_preflight_failure_replaces_stale_success_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cwd = Path(temporary_directory)
            (cwd / adapter.NATIVE_OUTPUT_NAME).write_text("stale result\n", encoding="utf-8")
            (cwd / adapter.MANIFEST_NAME).write_text('{"status":"valid"}\n', encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                with mock.patch.dict(os.environ, {"RS_CS_LABEL": "client-1"}, clear=False):
                    with mock.patch("sys.stderr", io.StringIO()):
                        result = adapter.execute_sample(["--backend", "invalid"])
            finally:
                os.chdir(old_cwd)
            self.assertNotEqual(result, 0)
            self.assertFalse((cwd / adapter.NATIVE_OUTPUT_NAME).exists())
            manifest = json.loads((cwd / adapter.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "invalid")
            self.assertIn("failure", manifest)
            self.assertTrue((cwd / adapter.ERROR_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
