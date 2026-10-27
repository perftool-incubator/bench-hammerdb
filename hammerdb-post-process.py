#!/usr/bin/env python3
"""Validate a completed native run and write its NOPM/TPM CDM metrics."""

import json
import math
import os
import sys
from pathlib import Path

from hammerdb_common import (
    HAMMERDB_RELEASES,
    engine_id_from_label,
    measurement_interval,
    parse_cli,
    parse_native_output,
    validate_schema_output,
)


MANIFEST = Path("hammerdb-run-manifest.json")
NATIVE_OUTPUT = Path("hammerdb-native-output.txt")
SCHEMA_BUILD_OUTPUT = Path("hammerdb-schema-build.txt")
SCHEMA_CHECK_OUTPUT = Path("hammerdb-schema-check.txt")
SAMPLE_DATA_SCHEMA = "2021.04.12"


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("HammerDB manifest %s is missing or not finite" % label)
    return float(value)


def validate_outputs(
    manifest,
    native_text,
    schema_build_text,
    schema_check_text,
    iteration_args,
    engine_label=None,
):
    if not isinstance(manifest, dict) or manifest.get("status") != "valid":
        raise ValueError("HammerDB manifest does not mark this attempt valid")
    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict):
        raise ValueError("HammerDB manifest has no upstream provenance")
    release_version = upstream.get("hammerdb_version")
    release = HAMMERDB_RELEASES.get(release_version)
    if not isinstance(release, dict) or upstream.get("hammerdb_commit") != release["commit"]:
        raise ValueError("HammerDB manifest has a different upstream pin")
    architecture = upstream.get("architecture")
    if upstream.get("archive_sha256") != release["tarball_sha256"].get(architecture):
        raise ValueError("HammerDB manifest has a different upstream archive pin")
    if upstream.get("cli_version_check") != "HammerDB CLI v%s" % release_version:
        raise ValueError("HammerDB manifest does not contain a successful CLI version check")

    if engine_label is None:
        engine_label = os.environ.get("RS_CS_LABEL", "")
    engine = manifest.get("engine")
    expected_engine_id = engine_id_from_label(engine_label)
    if (
        not isinstance(engine, dict)
        or engine.get("label") != engine_label
        or engine.get("engine_id") != expected_engine_id
    ):
        raise ValueError("HammerDB manifest engine identity does not match this Rickshaw engine")

    requested = manifest.get("requested")
    if not isinstance(requested, dict):
        raise ValueError("HammerDB manifest has no requested benchmark parameters")
    backend = requested.get("backend")
    if backend not in ("mariadb", "postgresql"):
        raise ValueError("HammerDB manifest backend is unsupported")
    if requested.get("workload") != "TPROC-C":
        raise ValueError("HammerDB manifest workload is not native TPROC-C")
    for key in ("warehouses", "virtual_users", "rampup_minutes", "duration_minutes"):
        if type(requested.get(key)) is not int:
            raise ValueError("HammerDB manifest %s is not an integer" % key)
    if requested.get("monitor_virtual_users") != 1:
        raise ValueError("HammerDB manifest does not account for its timed monitor VU")
    if requested.get("total_virtual_users") != requested["virtual_users"] + 1:
        raise ValueError("HammerDB manifest total VUs do not match workload plus monitor VUs")
    for key in ("backend", "warehouses", "virtual_users", "rampup_minutes", "duration_minutes"):
        if requested.get(key) != getattr(iteration_args, key):
            raise ValueError("HammerDB manifest %s does not match the Rickshaw iteration parameters" % key)
    if requested.get("data_dir_option") != iteration_args.data_dir:
        raise ValueError("HammerDB manifest data-dir does not match the Rickshaw iteration parameters")
    if requested.get("log_dir_option") != iteration_args.log_dir:
        raise ValueError("HammerDB manifest log-dir does not match the Rickshaw iteration parameters")

    native = manifest.get("native")
    database = manifest.get("database")
    cleanup = manifest.get("cleanup")
    attempt = manifest.get("attempt")
    if not isinstance(native, dict) or not isinstance(database, dict):
        raise ValueError("HammerDB manifest lacks completed native or database records")
    if not isinstance(cleanup, dict) or not isinstance(attempt, dict):
        raise ValueError("HammerDB manifest lacks cleanup or attempt state")
    if native.get("return_code") != 0 or database.get("timed_run_return_code") != 0:
        raise ValueError("HammerDB native timed execution did not complete successfully")
    if database.get("schema_build_return_code") != 0 or database.get("schema_check_return_code") != 0:
        raise ValueError("HammerDB native schema build or check did not complete successfully")
    if database.get("schema_ready") is not True:
        raise ValueError("HammerDB schema was not marked ready before measurement")
    if database.get("schema_build_status") != "native TPROC-C schema marker and single schema VU succeeded":
        raise ValueError("HammerDB manifest lacks native schema-build success evidence")
    if database.get("schema_check_status") != "native TPROC-C schema and consistency checks succeeded":
        raise ValueError("HammerDB manifest lacks native schema-check success evidence")
    try:
        validate_schema_output(backend, "schema-build", schema_build_text)
        validate_schema_output(backend, "schema-check", schema_check_text)
    except ValueError as exc:
        raise ValueError("retained HammerDB schema output is invalid: %s" % exc) from exc
    if database.get("warehouse_count") != requested["warehouses"]:
        raise ValueError("HammerDB manifest warehouse count does not match the requested schema")
    if cleanup.get("completed") is not True or cleanup.get("owned_database_stopped") is not True:
        raise ValueError("HammerDB owned database cleanup is incomplete")
    if cleanup.get("owned_process_groups_stopped") is not True:
        raise ValueError("HammerDB owned process groups were not confirmed stopped")
    if cleanup.get("owned_data_removed") is not True:
        raise ValueError("HammerDB private database data was not removed after the sample")
    if cleanup.get("owned_transaction_logs_removed") is not True:
        raise ValueError("HammerDB private transaction logs were not removed after the sample")
    if cleanup.get("private_credentials_removed") is not True:
        raise ValueError("HammerDB private credentials and job state were not removed")
    if (iteration_args.data_dir == "." or iteration_args.log_dir == ".") and cleanup.get("mechanics_scratch_removed") is not True:
        raise ValueError("HammerDB mechanics scratch root was not removed after the sample")
    if attempt.get("interrupted") is not False or attempt.get("timed_out") is not False:
        raise ValueError("HammerDB manifest indicates an interrupted or timed-out attempt")
    if attempt.get("return_code") != 0:
        raise ValueError("HammerDB adapter did not return success")

    parsed = parse_native_output(
        native_text,
        backend,
        requested["warehouses"],
        requested["virtual_users"],
        requested["rampup_minutes"],
        requested["duration_minutes"],
    )
    recorded = native.get("result")
    if not isinstance(recorded, dict):
        raise ValueError("HammerDB manifest native result is missing")
    for key, value in parsed.items():
        if recorded.get(key) != value:
            raise ValueError("HammerDB native field %s does not match retained output" % key)

    measurement = manifest.get("measurement")
    if not isinstance(measurement, dict):
        raise ValueError("HammerDB manifest has no measurement period")
    window = measurement_interval(
        measurement.get("start_epoch_ns"),
        measurement.get("end_epoch_ns"),
        requested["duration_minutes"],
        measurement.get("start_monotonic_ns"),
        measurement.get("end_monotonic_ns"),
    )
    if measurement.get("begin_ms") != window["begin_ms"] or measurement.get("end_ms") != window["end_ms"]:
        raise ValueError("HammerDB manifest CDM interval does not match its output marker timestamps")
    elapsed = _number(measurement.get("elapsed_seconds"), "measurement elapsed seconds")
    if abs(elapsed - window["elapsed_seconds"]) > 1e-6:
        raise ValueError("HammerDB manifest measurement duration does not match monotonic marker timestamps")

    return {"native": parsed, "begin": window["begin_ms"], "end": window["end_ms"], "backend": backend}


def _load_cdm_metrics():
    toolbox_home = os.environ.get("TOOLBOX_HOME")
    if not toolbox_home:
        raise ValueError("HammerDB post-processing requires TOOLBOX_HOME")
    toolbox_python = Path(toolbox_home) / "python"
    if not toolbox_python.is_dir():
        raise ValueError("HammerDB post-processing cannot find %s" % toolbox_python)
    sys.path.append(str(toolbox_python))
    from toolbox.cdm_metrics import CDMMetrics

    return CDMMetrics


def build_sample_data(metrics_file):
    return {
        "rickshaw-bench-metric": {"schema": {"version": SAMPLE_DATA_SCHEMA}},
        "benchmark": "hammerdb",
        "primary-period": "measurement",
        "primary-metric": "nopm",
        "periods": [{"name": "measurement", "metric-files": [metrics_file]}],
    }


def main(argv=None):
    iteration_args = parse_cli(sys.argv[1:] if argv is None else argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    native_text = NATIVE_OUTPUT.read_text(encoding="utf-8", errors="replace")
    schema_build_text = SCHEMA_BUILD_OUTPUT.read_text(encoding="utf-8", errors="replace")
    schema_check_text = SCHEMA_CHECK_OUTPUT.read_text(encoding="utf-8", errors="replace")
    verified = validate_outputs(
        manifest,
        native_text,
        schema_build_text,
        schema_check_text,
        iteration_args,
    )
    native = verified["native"]
    backend = verified["backend"]

    CDMMetrics = _load_cdm_metrics()
    metrics = CDMMetrics()
    common_descriptor = {
        "source": "hammerdb",
        "class": "throughput",
        "type": "nopm",
        "default-aggregation": "sum",
    }
    metrics.log_sample(
        "0",
        common_descriptor,
        {},
        {"begin": verified["begin"], "end": verified["end"], "value": native["nopm"]},
    )
    tpm_source = "hammerdb-mariadb" if backend == "mariadb" else "hammerdb-postgresql"
    tpm_type = "mariadb-tpm" if backend == "mariadb" else "postgresql-tpm"
    metrics.log_sample(
        "0",
        {
            "source": tpm_source,
            "class": "throughput",
            "type": tpm_type,
            "default-aggregation": "sum",
        },
        {},
        {"begin": verified["begin"], "end": verified["end"], "value": native["tpm"]},
    )
    metric_file = metrics.finish_samples()
    output_dir = Path("postprocess")
    output_dir.mkdir(exist_ok=True)
    (output_dir / "post-process-data.json").write_text(
        json.dumps(build_sample_data(metric_file), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("HammerDB post-processing failed: %s" % exc, file=sys.stderr)
        sys.exit(1)
