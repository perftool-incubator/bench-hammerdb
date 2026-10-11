import importlib.util
import unittest
from pathlib import Path

from hammerdb_common import HAMMERDB_COMMIT, HAMMERDB_VERSION, measurement_interval, parse_native_output


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hammerdb_post_process", PROJECT_ROOT / "hammerdb-post-process.py")
post_process = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(post_process)


def sample_output():
    return (
        "Vuser 1:Beginning rampup time of 0 minutes\n"
        "Vuser 1:Rampup complete, Taking start Transaction Count.\n"
        "Vuser 1:Timing test period of 1 in minutes\n"
        "Vuser 1:Test complete, Taking end Transaction Count.\n"
        "Vuser 1:TEST RESULT : System achieved 1234.50 NOPM from 2345.60 MariaDB TPM\n"
        "Vuser 2:FINISHED SUCCESS\n"
        "Vuser 3:FINISHED SUCCESS\n"
        "Vuser 1:FINISHED SUCCESS\n"
        "ALL VIRTUAL USERS COMPLETE\n"
    )


def schema_build_output(backend="mariadb"):
    schema = "TPCC" if backend == "mariadb" else "HDB_012345"
    return "Vuser 1:%s SCHEMA COMPLETE\nVuser 1:FINISHED SUCCESS\n" % schema


def schema_check_output():
    return "Vuser 1:tpcc TPROC-C Schema has been checked successfully.\nVuser 1:FINISHED SUCCESS\n"


def valid_manifest():
    native_text = sample_output()
    result = parse_native_output(native_text, "mariadb", 1, 2, 0, 1)
    wall_start = 1_700_000_000_123_000_000
    wall_end = wall_start + 60_100_000_000
    mono_start = 900_000_000_000
    mono_end = mono_start + 60_050_000_000
    window = measurement_interval(wall_start, wall_end, 1, mono_start, mono_end)
    return {
        "status": "valid",
        "engine": {"label": "client-1", "engine_id": 1},
        "upstream": {
            "hammerdb_version": HAMMERDB_VERSION,
            "hammerdb_commit": HAMMERDB_COMMIT,
            "cli_version_check": "HammerDB CLI v6.0",
        },
        "requested": {
            "backend": "mariadb",
            "workload": "TPROC-C",
            "warehouses": 1,
            "virtual_users": 2,
            "monitor_virtual_users": 1,
            "total_virtual_users": 3,
            "rampup_minutes": 0,
            "duration_minutes": 1,
            "data_dir_option": ".",
            "log_dir_option": ".",
        },
        "native": {"return_code": 0, "result": result},
        "database": {
            "timed_run_return_code": 0,
            "schema_build_return_code": 0,
            "schema_check_return_code": 0,
            "schema_build_status": "native v6 TPROC-C schema marker and single schema VU succeeded",
            "schema_check_status": "native TPROC-C schema and consistency checks succeeded",
            "schema_ready": True,
            "warehouse_count": 1,
        },
        "cleanup": {
            "completed": True,
            "owned_database_stopped": True,
            "owned_process_groups_stopped": True,
            "owned_data_removed": True,
            "owned_transaction_logs_removed": True,
            "mechanics_scratch_removed": True,
            "private_credentials_removed": True,
        },
        "attempt": {"interrupted": False, "timed_out": False, "return_code": 0},
        "measurement": {
            **window,
            "start_epoch_ns": wall_start,
            "end_epoch_ns": wall_end,
            "start_monotonic_ns": mono_start,
            "end_monotonic_ns": mono_end,
        },
    }


def valid_iteration_args():
    return post_process.parse_cli(
        [
            "--backend=mariadb",
            "--warehouses=1",
            "--virtual-users=2",
            "--rampup-minutes=0",
            "--duration-minutes=1",
            "--data-dir=.",
            "--log-dir=.",
        ]
    )


def validate(manifest=None, native_text=None, build_text=None, check_text=None, args=None, engine_label="client-1"):
    return post_process.validate_outputs(
        valid_manifest() if manifest is None else manifest,
        sample_output() if native_text is None else native_text,
        schema_build_output() if build_text is None else build_text,
        schema_check_output() if check_text is None else check_text,
        valid_iteration_args() if args is None else args,
        engine_label=engine_label,
    )


class HammerDBPostProcessTests(unittest.TestCase):
    def test_valid_native_output_and_manifest_match(self):
        result = validate()
        self.assertEqual(result["native"]["nopm"], 1234.5)
        self.assertEqual(result["backend"], "mariadb")

    def test_iteration_parameters_must_match_the_run_manifest(self):
        mismatch = post_process.parse_cli(
            ["--backend=postgresql", "--warehouses=4", "--virtual-users=16", "--rampup-minutes=0", "--duration-minutes=1", "--data-dir=.", "--log-dir=."]
        )
        with self.assertRaisesRegex(ValueError, "backend does not match"):
            validate(args=mismatch)
        mismatch = post_process.parse_cli(
            ["--backend=mariadb", "--warehouses=2", "--virtual-users=2", "--rampup-minutes=0", "--duration-minutes=1", "--data-dir=.", "--log-dir=."]
        )
        with self.assertRaisesRegex(ValueError, "warehouses does not match"):
            validate(args=mismatch)
        mismatch = post_process.parse_cli(
            ["--backend=mariadb", "--warehouses=1", "--virtual-users=4", "--rampup-minutes=0", "--duration-minutes=1", "--data-dir=.", "--log-dir=."]
        )
        with self.assertRaisesRegex(ValueError, "virtual_users does not match"):
            validate(args=mismatch)

    def test_status_or_native_tampering_prevents_indexing(self):
        manifest = valid_manifest()
        manifest["status"] = "invalid"
        with self.assertRaisesRegex(ValueError, "does not mark this attempt valid"):
            validate(manifest=manifest)
        manifest = valid_manifest()
        manifest["native"]["result"]["nopm"] = 1
        with self.assertRaisesRegex(ValueError, "does not match retained output"):
            validate(manifest=manifest)

    def test_cleanup_and_timing_are_required_before_indexing(self):
        manifest = valid_manifest()
        manifest["cleanup"]["owned_data_removed"] = False
        with self.assertRaisesRegex(ValueError, "private database data"):
            validate(manifest=manifest)
        manifest = valid_manifest()
        manifest["measurement"]["elapsed_seconds"] = 5
        with self.assertRaisesRegex(ValueError, "monotonic marker timestamps"):
            validate(manifest=manifest)

    def test_schema_outputs_and_engine_identity_are_revalidated(self):
        with self.assertRaisesRegex(ValueError, "retained HammerDB schema output"):
            validate(build_text="Vuser 1:FINISHED SUCCESS\n")
        manifest = valid_manifest()
        manifest["engine"]["label"] = "client-2"
        manifest["engine"]["engine_id"] = 2
        with self.assertRaisesRegex(ValueError, "does not match this Rickshaw engine"):
            validate(manifest=manifest, engine_label="client-1")

    def test_tpm_types_are_backend_specific(self):
        self.assertEqual(post_process.build_sample_data("metric.json")["primary-metric"], "nopm")
        source = (PROJECT_ROOT / "benchmark-metadata.json").read_text(encoding="utf-8")
        self.assertIn("mariadb-tpm", source)
        self.assertIn("postgresql-tpm", source)


if __name__ == "__main__":
    unittest.main()
