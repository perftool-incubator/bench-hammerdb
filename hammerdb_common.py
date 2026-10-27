#!/usr/bin/env python3
"""Shared contracts and validation for the Crucible HammerDB adapter."""

import argparse
import json
import math
import os
import re
import shutil
import socket
import time
from pathlib import Path


HAMMERDB_RELEASES = {
    "5.0": {
        "commit": "22ded31bf47b2ffe25263d686c0fc630965cfb76",
        "home": Path("/opt/crucible/software/HammerDB-5.0"),
        "tarball_sha256": {
            "x86_64": "e547d11faaeec54b721cd661683a020c241f4474f11ece8803564cb17abd86d3",
        },
    },
    "6.0": {
        "commit": "d33f879aec858063edd17aa2daa46db03abb2bae",
        "home": Path("/opt/crucible/software/HammerDB-6.0"),
        "tarball_sha256": {
            "x86_64": "b4e0b1c84f9ac74802c86628b3ebca527ef314404be68827fca30174147a92b6",
            "aarch64": "6f6687ff3f9545dee2a1d2844a236a6df71a446621f750dad66dfd828ea5a4c9",
        },
    },
}
HAMMERDB_ACTIVE_HOME = Path("/opt/crucible/software/HammerDB")


def _hammerdb_release_for_home(home):
    candidate = Path(home)
    for version, release in HAMMERDB_RELEASES.items():
        if candidate == release["home"]:
            return version, release
    return "6.0", HAMMERDB_RELEASES["6.0"]


try:
    _hammerdb_active_home = HAMMERDB_ACTIVE_HOME.resolve(strict=True)
except OSError:
    _hammerdb_active_home = HAMMERDB_RELEASES["6.0"]["home"]

HAMMERDB_VERSION, _HAMMERDB_RELEASE = _hammerdb_release_for_home(_hammerdb_active_home)
HAMMERDB_COMMIT = _HAMMERDB_RELEASE["commit"]
HAMMERDB_TARBALL_SHA256 = _HAMMERDB_RELEASE["tarball_sha256"]
HAMMERDB_HOME = _HAMMERDB_RELEASE["home"]
HAMMERDB_CLI = HAMMERDB_HOME / "hammerdbcli"

DEFAULT_WAREHOUSES = 1
MAX_WAREHOUSES = 4
DEFAULT_VIRTUAL_USERS = 2
MAX_VIRTUAL_USERS = 16
DEFAULT_RAMPUP_MINUTES = 1
MAX_RAMPUP_MINUTES = 30
DEFAULT_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 60
MAX_TOTAL_ITERATIONS = 2147483647

MAX_DATABASE_START_SECONDS = 120
MAX_SCHEMA_BUILD_SECONDS = 1800
MAX_SCHEMA_CHECK_SECONDS = 600
MAX_HAMMERDB_CLEANUP_SECONDS = 60
DATABASE_SHUTDOWN_SECONDS = 30
MIN_MARIADB_REDO_BYTES = 128 * 1024 * 1024
MIN_POSTGRESQL_WAL_BYTES = 2 * 1024 * 1024 * 1024
RUNTIME_FIXED_SECONDS = (
    20  # HammerDB CLI identification
    + MAX_DATABASE_START_SECONDS  # Database initialization
    + MAX_DATABASE_START_SECONDS  # Database readiness
    + 20  # Database version and package provenance
    + 40  # Database login creation/readiness
    + 120  # Native database-driver load check
    + MAX_SCHEMA_BUILD_SECONDS
    + MAX_SCHEMA_CHECK_SECONDS
    + MAX_HAMMERDB_CLEANUP_SECONDS
    + 120  # Timed CLI process/shutdown allowance
    + DATABASE_SHUTDOWN_SECONDS
    + 180  # UID preflight, process termination, provenance and bounded cleanup allowance
)

DATA_ATTEMPT_PREFIX = ".hammerdb-data-"
LOG_ATTEMPT_PREFIX = "hammerdb-logs-"
MANIFEST_NAME = "hammerdb-run-manifest.json"
NATIVE_OUTPUT_NAME = "hammerdb-native-output.txt"
DATABASE_LOG_NAME = "hammerdb-database-server.log"
ERROR_NAME = "hammerdb-error.txt"

_RESULT_RE = re.compile(
    r"^\s*Vuser\s+1:TEST RESULT\s*:\s*System achieved\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+NOPM\s+from\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+(MariaDB|PostgreSQL)\s+TPM\s*$",
    re.IGNORECASE,
)
_RAMPUP_RE = re.compile(r"^\s*Vuser\s+1:Beginning rampup time of ([0-9]+) minutes\s*$")
_DURATION_RE = re.compile(r"^\s*Vuser\s+1:Timing test period of ([0-9]+) in minutes\s*$")
_VUSER_FINISHED_RE = re.compile(r"^\s*Vuser\s+([0-9]+):FINISHED (SUCCESS|FAILED)\s*$")
_ENGINE_LABEL_RE = re.compile(r"^client-([1-9][0-9]*)$")


class RunInterrupted(RuntimeError):
    """Crucible or the operating system interrupted the adapter."""


class RunTimedOut(RuntimeError):
    """An owned setup or workload process exceeded its bounded timeout."""


def parse_cli(argv):
    parser = argparse.ArgumentParser(description="Run one isolated HammerDB TPROC-C sample")
    parser.add_argument("--backend", choices=("mariadb", "postgresql"), default="mariadb")
    parser.add_argument("--warehouses", type=int, default=DEFAULT_WAREHOUSES)
    parser.add_argument("--virtual-users", type=int, default=DEFAULT_VIRTUAL_USERS)
    parser.add_argument("--rampup-minutes", type=int, default=DEFAULT_RAMPUP_MINUTES)
    parser.add_argument("--duration-minutes", type=int, default=DEFAULT_DURATION_MINUTES)
    parser.add_argument("--data-dir", default=".")
    parser.add_argument("--log-dir", default=".")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        raise ValueError("unsupported HammerDB option(s): " + " ".join(unknown))
    if not 1 <= args.warehouses <= MAX_WAREHOUSES:
        raise ValueError("warehouses must be between 1 and %d" % MAX_WAREHOUSES)
    if not 1 <= args.virtual_users <= MAX_VIRTUAL_USERS:
        raise ValueError("virtual-users must be between 1 and %d" % MAX_VIRTUAL_USERS)
    if not 0 <= args.rampup_minutes <= MAX_RAMPUP_MINUTES:
        raise ValueError("rampup-minutes must be between 0 and %d" % MAX_RAMPUP_MINUTES)
    if not 1 <= args.duration_minutes <= MAX_DURATION_MINUTES:
        raise ValueError("duration-minutes must be between 1 and %d" % MAX_DURATION_MINUTES)
    return args


def runtime_seconds(args):
    """Return a safe maximum bound, including DB and schema preparation."""
    return (
        RUNTIME_FIXED_SECONDS
        + args.rampup_minutes * 60
        + args.duration_minutes * 60
    )


def engine_id_from_label(value):
    match = _ENGINE_LABEL_RE.fullmatch(value or "")
    if not match:
        raise ValueError("RS_CS_LABEL must be a Crucible client label such as client-1")
    engine_id = int(match.group(1))
    # Keep generated listener ports below 61000 and away from the privileged range.
    if engine_id > 10499:
        raise ValueError("client id is outside the supported database port range")
    return engine_id


def validate_schema_output(backend, mode, output):
    """Validate pinned HammerDB v6 native schema markers and VU status."""
    if backend not in ("mariadb", "postgresql"):
        raise ValueError("unsupported backend in HammerDB schema output")
    if mode not in ("schema-build", "schema-check"):
        raise ValueError("unsupported HammerDB schema operation")
    if not isinstance(output, str) or not output.strip():
        raise ValueError("HammerDB native %s output is empty" % mode)
    if re.search(
        r"(?im)^\s*(?:ERROR|FATAL)(?:\b|:)|"
        r"^\s*Vuser\s+\d+:FINISHED FAILED\b|"
        r"^\s*HAMMERDB_SCHEMA_(?:BUILD|CHECK)_ERROR:",
        output,
    ):
        raise ValueError("HammerDB native %s output contains a failed operation" % mode)

    statuses = {}
    for match in re.finditer(r"(?im)^Vuser\s+(\d+):FINISHED (SUCCESS|FAILED)\s*$", output):
        vuser = int(match.group(1))
        if vuser in statuses:
            raise ValueError("HammerDB native %s reports a virtual user more than once" % mode)
        statuses[vuser] = match.group(2).upper()
    if statuses != {1: "SUCCESS"}:
        raise ValueError("HammerDB native %s did not finish its single schema VU successfully" % mode)

    if mode == "schema-build":
        schema_name = r"TPCC" if backend == "mariadb" else r"HDB_[A-Z0-9_]+"
        marker = r"(?im)^Vuser 1:%s SCHEMA COMPLETE\s*$" % schema_name
        if not re.search(marker, output):
            raise ValueError("HammerDB did not report the pinned native %s schema-build marker" % backend)
    else:
        if not re.search(
            r"(?im)^Vuser 1:tpcc TPROC-C Schema has been checked successfully\.\s*$",
            output,
        ):
            raise ValueError("HammerDB did not report successful native TPROC-C schema checks")


def process_group_has_live_members(group_id):
    """Return whether a process group still contains a process that can run."""
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2 :].split()
            # /proc/<pid>/stat fields after comm start with state, ppid,
            # pgrp. Zombies have stopped executing and no longer hold files.
            if len(fields) > 2 and int(fields[2]) == group_id and fields[0] not in ("Z", "X"):
                return True
        except (OSError, ValueError):
            continue
    return False


def process_group_is_stopped(group_id):
    """Return true only when no runnable or uninterruptible group member remains."""
    return not process_group_has_live_members(group_id)


def database_port(engine_id, backend):
    if not 1 <= engine_id <= 10499:
        raise ValueError("client id is outside the supported database port range")
    offset = 0 if backend == "mariadb" else 1
    return 40000 + (engine_id - 1) * 2 + offset


def port_is_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Allow a fresh daemon to bind a port whose previous TCP connections
        # are still in TIME_WAIT. Calling listen() as well as bind() preserves
        # the live-listener check when both sockets enable SO_REUSEADDR.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.listen(1)
        except OSError:
            return False
    return True


def _unescape_mount_field(value):
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def mount_provenance(path):
    """Return the visible Linux mount backing ``path`` from mountinfo."""
    resolved = Path(path).resolve(strict=True)
    try:
        info = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("cannot inspect /proc/self/mountinfo for storage provenance") from exc

    candidates = []
    for line in info.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        right = after.split()
        if len(fields) < 6 or len(right) < 2:
            continue
        target = Path(_unescape_mount_field(fields[4]))
        try:
            resolved.relative_to(target)
        except ValueError:
            continue
        candidates.append((len(str(target)), fields, right, target))
    if not candidates:
        raise ValueError("no visible mount contains storage path: %s" % resolved)
    _, fields, right, target = max(candidates, key=lambda item: item[0])
    stat_result = resolved.stat()
    statvfs_result = os.statvfs(resolved)
    return {
        "path": str(resolved),
        "mount_id": int(fields[0]),
        "device_major_minor": fields[2],
        "mount_root": _unescape_mount_field(fields[3]),
        "mount_target": str(target),
        "filesystem_type": right[0],
        "source": _unescape_mount_field(right[1]),
        "filesystem_device": stat_result.st_dev,
        "exact_mountpoint": resolved == target,
        "free_bytes": statvfs_result.f_bavail * statvfs_result.f_frsize,
        "total_bytes": statvfs_result.f_blocks * statvfs_result.f_frsize,
    }


def storage_root(value, cwd=None, require_mount=True):
    """Resolve an existing storage root without following a user-supplied symlink.

    An explicit root must be an absolute path and an actual engine mountpoint.
    The caller maps the mechanics-only ``.`` option to a fresh private scratch
    root before calling this helper. The adapter creates only a unique child
    under the resolved path.
    """
    base = Path(cwd or os.getcwd()).resolve(strict=True)
    if value == ".":
        return base
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError("explicit data-dir and log-dir values must be absolute paths")
    if candidate == Path("/"):
        raise ValueError("storage root cannot be the filesystem root")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        raise ValueError("storage paths must use absolute ASCII letters, digits, slash, dot, underscore, and hyphen")
    if candidate.is_symlink():
        raise ValueError("storage roots cannot be symlinks")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("storage root does not exist: %s" % candidate) from exc
    if not root.is_dir():
        raise ValueError("storage root is not a directory: %s" % root)
    if root == Path("/"):
        raise ValueError("storage root cannot be the filesystem root")
    if require_mount and not mount_provenance(root)["exact_mountpoint"]:
        raise ValueError("explicit storage root must be an engine mountpoint: %s" % root)
    return root


def _cgroup_memory_snapshot():
    """Inspect memory limits on the leaf and every visible cgroup ancestor."""
    try:
        cgroup_lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        mount_lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    hierarchy = None
    for line in cgroup_lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and not fields[1]:
            hierarchy = ("v2", fields[2])
            break
    if hierarchy is None:
        for line in cgroup_lines:
            fields = line.split(":", 2)
            if len(fields) == 3 and "memory" in fields[1].split(","):
                hierarchy = ("v1", fields[2])
                break
    if hierarchy is None:
        return []

    version, cgroup_path = hierarchy
    mounts = []
    for line in mount_lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        right = after.split()
        if len(fields) < 5 or not right:
            continue
        if (version == "v2" and right[0] == "cgroup2") or (
            version == "v1"
            and right[0] == "cgroup"
            and "memory" in ",".join(right[2:]).split(",")
        ):
            mount_root = Path(_unescape_mount_field(fields[3]))
            mount_point = Path(_unescape_mount_field(fields[4]))
            mounts.append((mount_root, mount_point))
    if not mounts:
        return []

    cg_path = Path(cgroup_path)
    selected = None
    for mount_root, mount_point in mounts:
        try:
            suffix = cg_path.relative_to(mount_root)
        except ValueError:
            if mount_root == Path("/"):
                suffix = Path(str(cg_path).lstrip("/"))
            else:
                continue
        selected = (mount_point / suffix, mount_point)
        break
    if selected is None:
        return []

    leaf, mount_point = selected
    directories = []
    current = leaf
    while True:
        directories.append(current)
        if current == mount_point or current.parent == current:
            break
        try:
            current.relative_to(mount_point)
        except ValueError:
            break
        current = current.parent

    limits = []
    for directory in directories:
        if version == "v2":
            limit_file = directory / "memory.max"
            usage_file = directory / "memory.current"
        else:
            limit_file = directory / "memory.limit_in_bytes"
            usage_file = directory / "memory.usage_in_bytes"
        try:
            limit_text = limit_file.read_text(encoding="ascii").strip()
            usage = int(usage_file.read_text(encoding="ascii").strip())
            if limit_text == "max":
                continue
            limit = int(limit_text)
            if limit >= (1 << 60):
                continue
            limits.append(
                {
                    "path": str(directory),
                    "limit_bytes": limit,
                    "current_bytes": usage,
                    "remaining_bytes": max(0, limit - usage),
                }
            )
        except (OSError, ValueError):
            continue
    return limits


def memory_availability_snapshot():
    """Return host and visible cgroup availability evidence for a preflight."""
    host_available = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                host_available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass

    cgroup_limits = _cgroup_memory_snapshot()
    cgroup_available = min((limit["remaining_bytes"] for limit in cgroup_limits), default=None)
    candidates = [value for value in (host_available, cgroup_available) if value is not None]
    if not candidates:
        raise ValueError("could not determine available host or cgroup memory")
    return {
        "host_mem_available_bytes": host_available,
        "visible_cgroup_limits": cgroup_limits,
        "visible_cgroup_available_bytes": cgroup_available,
        "effective_snapshot_available_bytes": min(candidates),
        "cgroup_scope": "leaf and visible ancestors only; hidden ancestor limits may not be observable",
    }


def effective_available_memory():
    """Return the lower of host MemAvailable and visible cgroup remaining memory."""
    return memory_availability_snapshot()["effective_snapshot_available_bytes"]


def required_memory_bytes(args):
    # Database buffers are fixed at 128 MiB; leave headroom for HammerDB, schema
    # build workers and per-connection state rather than scaling from host RAM.
    return (512 + 16 * args.virtual_users + 64 * args.warehouses) * 1024 * 1024


def required_disk_bytes(args):
    # Reserve substantial space for the generated database files. Transaction
    # logs have a separate mount contract and a separate minimum below.
    return (1024 + 768 * args.warehouses) * 1024 * 1024


def required_transaction_log_bytes(args):
    # MariaDB uses a 64 MiB redo group. PostgreSQL max_wal_size is configured
    # to 1 GiB, but is a soft limit; reserve 2 GiB as a heuristic, not a cap.
    return MIN_MARIADB_REDO_BYTES if args.backend == "mariadb" else MIN_POSTGRESQL_WAL_BYTES


def validate_resources(args, data_root, log_root):
    memory_snapshot = memory_availability_snapshot()
    available_memory = memory_snapshot["effective_snapshot_available_bytes"]
    required_memory = required_memory_bytes(args)
    if available_memory < required_memory:
        raise ValueError(
            "effective available memory is %d MiB; this selection requires at least %d MiB"
            % (available_memory // (1024 * 1024), required_memory // (1024 * 1024))
        )
    available_disk = shutil.disk_usage(data_root).free
    available_log_disk = shutil.disk_usage(log_root).free
    required_disk = required_disk_bytes(args)
    required_log_disk = required_transaction_log_bytes(args)
    same_filesystem = os.stat(data_root).st_dev == os.stat(log_root).st_dev
    required_combined_disk = required_disk + required_log_disk if same_filesystem else None
    if same_filesystem and available_disk < required_combined_disk:
        raise ValueError(
            "data and transaction-log mounts share a filesystem with %d MiB free; this selection requires at least %d MiB combined"
            % (available_disk // (1024 * 1024), required_combined_disk // (1024 * 1024))
        )
    if not same_filesystem and available_disk < required_disk:
        raise ValueError(
            "data mount has %d MiB free; this selection requires at least %d MiB"
            % (available_disk // (1024 * 1024), required_disk // (1024 * 1024))
        )
    if not same_filesystem and available_log_disk < required_log_disk:
        raise ValueError(
            "transaction-log mount has %d MiB free; this selection requires at least %d MiB"
            % (available_log_disk // (1024 * 1024), required_log_disk // (1024 * 1024))
        )
    return {
        "available_memory_bytes": available_memory,
        "minimum_memory_bytes": required_memory,
        "available_data_bytes": available_disk,
        "minimum_data_bytes": required_disk,
        "available_log_bytes": available_log_disk,
        "minimum_log_bytes": required_log_disk,
        "data_and_log_share_filesystem": same_filesystem,
        "minimum_combined_bytes": required_combined_disk,
        "memory_snapshot_method": "heuristic snapshot of host MemAvailable and visible cgroup ancestor limits",
        "memory_snapshot": memory_snapshot,
        "storage_snapshot_is_not_a_capacity_guarantee": True,
    }


def parse_native_output(text, backend, warehouses, virtual_users, rampup_minutes, duration_minutes):
    """Validate HammerDB's native timed TPROC-C result and VU completion."""
    if backend not in ("mariadb", "postgresql"):
        raise ValueError("unsupported backend in HammerDB native output")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("HammerDB native output is empty")

    lines = text.splitlines()
    results = [(index, match) for index, line in enumerate(lines) if (match := _RESULT_RE.fullmatch(line))]
    if len(results) != 1:
        raise ValueError("HammerDB output must contain exactly one native TPROC-C result")
    result_index, result_match = results[0]
    expected_name = "MariaDB" if backend == "mariadb" else "PostgreSQL"
    if result_match.group(3).lower() != expected_name.lower():
        raise ValueError("HammerDB native result identifies a different database backend")
    nopm = float(result_match.group(1))
    tpm = float(result_match.group(2))
    if not math.isfinite(nopm) or nopm <= 0 or not math.isfinite(tpm) or tpm <= 0:
        raise ValueError("HammerDB native NOPM and TPM must be finite positive values")

    rampup_records = [(i, m) for i, line in enumerate(lines) if (m := _RAMPUP_RE.fullmatch(line))]
    duration_records = [(i, m) for i, line in enumerate(lines) if (m := _DURATION_RE.fullmatch(line))]
    if len(rampup_records) != 1 or int(rampup_records[0][1].group(1)) != rampup_minutes:
        raise ValueError("HammerDB output does not match the requested ramp-up period")
    if len(duration_records) != 1 or int(duration_records[0][1].group(1)) != duration_minutes:
        raise ValueError("HammerDB output does not match the requested measurement duration")

    start_markers = [
        i for i, line in enumerate(lines)
        if "Vuser 1:Rampup complete, Taking start Transaction Count." in line
    ]
    end_markers = [
        i for i, line in enumerate(lines)
        if "Vuser 1:Test complete, Taking end Transaction Count." in line
    ]
    if len(start_markers) != 1 or len(end_markers) != 1:
        raise ValueError("HammerDB monitor did not report exactly one pair of counter-window markers")
    begin_index, end_index = start_markers[0], end_markers[0]
    if not begin_index < end_index < result_index:
        raise ValueError("HammerDB monitor counter-window markers are out of order")
    if not any(index > begin_index and _DURATION_RE.fullmatch(lines[index]) for index in range(len(lines))):
        raise ValueError("HammerDB did not announce the requested timed window after ramp-up")

    expected_workers = set(range(2, virtual_users + 2))
    worker_status = {}
    for index, line in enumerate(lines):
        match = _VUSER_FINISHED_RE.fullmatch(line)
        if not match:
            continue
        user_id = int(match.group(1))
        status = match.group(2)
        if user_id == 1:
            continue
        if user_id in worker_status:
            raise ValueError("HammerDB reported a worker VU completion more than once")
        worker_status[user_id] = {"status": status, "line": index}
        if status != "SUCCESS":
            raise ValueError("HammerDB workload VU %d reported failure" % user_id)
        if index < end_index:
            raise ValueError("HammerDB workload VU %d finished before the timed window ended" % user_id)
    if set(worker_status) != expected_workers:
        raise ValueError("HammerDB did not complete every configured workload VU")
    monitor_status = [
        match.group(2)
        for line in lines
        if (match := _VUSER_FINISHED_RE.fullmatch(line)) and int(match.group(1)) == 1
    ]
    if monitor_status != ["SUCCESS"]:
        raise ValueError("HammerDB monitor VU did not finish successfully")
    if text.count("ALL VIRTUAL USERS COMPLETE") != 1:
        raise ValueError("HammerDB did not report completion of all virtual users")
    if re.search(r"(?im)^\s*(?:ERROR:|FATAL:|Vuser\s+[0-9]+:FINISHED FAILED)", text):
        raise ValueError("HammerDB output contains a native workload error")

    return {
        "nopm": nopm,
        "tpm": tpm,
        "tpm_backend": expected_name,
        "workload_virtual_users": virtual_users,
        "monitor_virtual_users": 1,
        "total_virtual_users": virtual_users + 1,
        "warehouses": warehouses,
        "rampup_minutes": rampup_minutes,
        "duration_minutes": duration_minutes,
        "measurement_start_marker_line": begin_index + 1,
        "measurement_end_marker_line": end_index + 1,
        "native_result_line": lines[result_index].strip(),
    }


def measurement_interval(
    start_epoch_ns,
    end_epoch_ns,
    duration_minutes,
    start_monotonic_ns=None,
    end_monotonic_ns=None,
):
    """Return wall-clock CDM bounds while validating elapsed time monotonically."""
    if type(start_epoch_ns) is not int or type(end_epoch_ns) is not int:
        raise ValueError("measurement marker receipt timestamps are missing")
    if start_epoch_ns <= 0 or end_epoch_ns <= start_epoch_ns:
        raise ValueError("measurement marker receipt timestamps are not ordered")
    start_elapsed_ns = start_monotonic_ns if type(start_monotonic_ns) is int else start_epoch_ns
    end_elapsed_ns = end_monotonic_ns if type(end_monotonic_ns) is int else end_epoch_ns
    if start_elapsed_ns <= 0 or end_elapsed_ns <= start_elapsed_ns:
        raise ValueError("HammerDB monitor elapsed-time markers are not ordered")
    elapsed_seconds = (end_elapsed_ns - start_elapsed_ns) / 1_000_000_000
    expected_seconds = duration_minutes * 60
    tolerance_seconds = max(5.0, expected_seconds * 0.05)
    if abs(elapsed_seconds - expected_seconds) > tolerance_seconds:
        raise ValueError(
            "observed HammerDB monitor window %.1f seconds differs from configured %d seconds"
            % (elapsed_seconds, expected_seconds)
        )
    begin_ms = start_epoch_ns // 1_000_000
    end_ms = (end_epoch_ns + 999_999) // 1_000_000
    return {
        "begin_ms": begin_ms,
        "end_ms": end_ms,
        "elapsed_seconds": elapsed_seconds,
        "configured_seconds": expected_seconds,
        "timestamp_source": "adapter wall-clock receipt of HammerDB monitor counter-window output",
        "boundary_uncertainty": "stdout marker receipt can lag the native counter snapshot",
    }


def atomic_json(path, data, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def redact_secrets(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def write_private_text(path, text):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", errors="replace")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def process_marker(process):
    """Return Linux process identity fields for an owned live child."""
    pid = process.pid
    return_code = process.poll()
    marker = {"pid": pid, "alive": return_code is None}
    if return_code is not None:
        marker["return_code"] = return_code
    executable_path = "/proc/%d/exe" % pid
    try:
        cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        # realpath() can return the unresolved /proc link without raising when
        # access is restricted, which looks like an unexpected executable to
        # callers. Read the link explicitly so the existing fallback to Popen
        # metadata handles that case.
        executable = os.path.realpath(os.readlink(executable_path))
    except OSError as exc:
        # /proc access can be restricted when this process supervises a
        # database running as its service account. The Popen handle remains
        # authoritative for whether our owned child is still alive.
        marker["proc_inspection_error"] = "%s: %s" % (type(exc).__name__, exc)
        return marker
    try:
        process_group = os.getpgid(pid)
    except OSError:
        process_group = None
    marker.update(
        {
            "process_group": process_group,
            "executable": executable,
            "command_line": cmdline,
        }
    )
    return marker


def database_version_command(backend):
    if backend == "mariadb":
        for executable in ("mariadb", "mysql"):
            path = shutil.which(executable)
            if path:
                return [path, "--version"]
        return ["mariadb", "--version"]
    return ["postgres", "--version"]


def check_port_range():
    return 40000, 60999


def current_epoch_ns():
    return time.time_ns()
