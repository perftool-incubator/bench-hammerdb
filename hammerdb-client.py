#!/usr/bin/env python3
"""Run a bounded local MariaDB or PostgreSQL HammerDB TPROC-C sample."""

import json
import os
import pwd
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from hammerdb_common import (
    DATABASE_LOG_NAME,
    DATABASE_SHUTDOWN_SECONDS,
    DATA_ATTEMPT_PREFIX,
    ERROR_NAME,
    HAMMERDB_CLI,
    HAMMERDB_COMMIT,
    HAMMERDB_HOME,
    HAMMERDB_TARBALL_SHA256,
    HAMMERDB_VERSION,
    LOG_ATTEMPT_PREFIX,
    MANIFEST_NAME,
    MAX_DATABASE_START_SECONDS,
    MAX_HAMMERDB_CLEANUP_SECONDS,
    MAX_SCHEMA_BUILD_SECONDS,
    MAX_SCHEMA_CHECK_SECONDS,
    MAX_TOTAL_ITERATIONS,
    NATIVE_OUTPUT_NAME,
    RunInterrupted,
    RunTimedOut,
    atomic_json,
    current_epoch_ns,
    database_port,
    database_version_command,
    engine_id_from_label,
    measurement_interval,
    mount_provenance,
    parse_cli,
    parse_native_output,
    port_is_available,
    process_marker,
    process_group_has_live_members,
    process_group_is_stopped,
    redact_secrets,
    runtime_seconds,
    storage_root,
    validate_resources,
    validate_schema_output as validate_native_schema_output,
    write_private_text,
)


ACTIVE_PROCESSES = set()
SIGNAL_RECEIVED = None
ARTIFACT_FILES = (
    MANIFEST_NAME,
    NATIVE_OUTPUT_NAME,
    DATABASE_LOG_NAME,
    ERROR_NAME,
    "hammerdb-schema-build.txt",
    "hammerdb-schema-check.txt",
    "hammerdb-driver-check.txt",
)

HAMMERDB_STAGE_ARTIFACTS = {
    "driver-check": "hammerdb-driver-check.txt",
    "schema-build": "hammerdb-schema-build.txt",
    "schema-check": "hammerdb-schema-check.txt",
    "timed-run": NATIVE_OUTPUT_NAME,
}
MAX_STAGE_OUTPUT_BYTES = 16 * 1024 * 1024


def handle_signal(signum, _frame):
    global SIGNAL_RECEIVED
    if SIGNAL_RECEIVED is None:
        SIGNAL_RECEIVED = signum


def raise_if_interrupted():
    if SIGNAL_RECEIVED is not None:
        raise RunInterrupted("run interrupted by signal %s" % signal.Signals(SIGNAL_RECEIVED).name)


def terminate_process_group(process, grace_seconds=8):
    if process is None:
        return
    group_id = process.pid

    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while process_group_has_live_members(group_id) and time.monotonic() < deadline:
        process.poll()  # reap the direct child before probing for group leftovers
        time.sleep(0.05)
    if process_group_has_live_members(group_id):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        kill_deadline = time.monotonic() + 2
        while process_group_has_live_members(group_id) and time.monotonic() < kill_deadline:
            time.sleep(0.05)
        if process_group_has_live_members(group_id):
            raise RuntimeError("owned process group %d remained after SIGKILL" % group_id)
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("owned process %d remained after group termination" % process.pid) from exc


def _register_process(process):
    ACTIVE_PROCESSES.add(process)


def _unregister_process(process):
    ACTIVE_PROCESSES.discard(process)


def _decode_output(data):
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", "replace")


def run_command(
    command,
    timeout,
    label,
    input_text=None,
    env=None,
    cwd=None,
    secrets_to_redact=(),
    user_account=None,
):
    """Run one short-lived owned command with a process-group timeout."""
    raise_if_interrupted()
    popen_options = {}
    if user_account is not None:
        popen_options.update(
            user=user_account.pw_uid,
            group=user_account.pw_gid,
            extra_groups=[],
        )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        env=env,
        start_new_session=True,
        close_fds=True,
        **popen_options,
    )
    _register_process(process)
    deadline = time.monotonic() + timeout
    next_input = input_text
    try:
        while True:
            if SIGNAL_RECEIVED is not None:
                terminate_process_group(process)
                raise_if_interrupted()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(process)
                raise RunTimedOut("%s exceeded its %d second limit" % (label, timeout))
            try:
                output, _ = process.communicate(input=next_input, timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                next_input = None
                continue
        output = redact_secrets(_decode_output(output), secrets_to_redact)
        if process.returncode != 0:
            detail = output[-4000:].strip()
            raise RuntimeError(
                "%s exited with status %d%s"
                % (label, process.returncode, (": " + detail) if detail else "")
            )
        return output
    finally:
        terminate_process_group(process)
        _unregister_process(process)


def require_executable(name):
    value = shutil.which(name)
    if not value:
        raise RuntimeError("required prepared executable is missing: %s" % name)
    return Path(value).resolve()


def require_service_account(name):
    try:
        account = pwd.getpwnam(name)
    except KeyError as exc:
        raise RuntimeError("prepared database service account is missing: %s" % name) from exc
    return account


def chown_private_tree(path, account):
    os.chown(path, account.pw_uid, account.pw_gid)
    os.chmod(path, 0o700)


def sql_literal(value):
    if not re.fullmatch(r"[a-zA-Z0-9_]+", value):
        raise ValueError("generated SQL identifier contains unexpected characters")
    return "'" + value + "'"


def tcl_braced(value):
    value = str(value)
    if any(character in value for character in "{}\\\r\n"):
        raise ValueError("generated Tcl value contains an unsupported brace, backslash, or newline")
    return "{" + value + "}"


def process_is_our_database(process, backend, datadir, port):
    marker = process_marker(process)
    if not marker.get("alive"):
        return_code = marker.get("return_code")
        detail = " with return code %s" % return_code if return_code is not None else ""
        raise RuntimeError("the owned database process exited before becoming ready%s" % detail)
    command_args = process.args
    if isinstance(command_args, (str, bytes)):
        command_args = [os.fsdecode(command_args)]
    else:
        command_args = [os.fsdecode(argument) for argument in command_args]
    command_line = marker.get("command_line") or " ".join(command_args)
    if str(datadir) not in command_line or str(port) not in command_line:
        raise RuntimeError("the live database PID does not match its owned data directory and port")
    executable = marker.get("executable") or (command_args[0] if command_args else "")
    if backend == "mariadb" and "maria" not in Path(executable).name.lower():
        raise RuntimeError("the live PID executable is not the expected MariaDB daemon")
    if backend == "postgresql" and Path(executable).name != "postgres":
        raise RuntimeError("the live PID executable is not the expected PostgreSQL daemon")
    marker["command_line"] = command_line
    marker["executable"] = executable
    return marker


class LocalDatabase:
    def __init__(self, backend, data_attempt, log_attempt, port, virtual_users):
        self.backend = backend
        self.data_attempt = Path(data_attempt)
        self.log_attempt = Path(log_attempt)
        self.port = port
        self.virtual_users = virtual_users
        self.process = None
        self.log_stream = None
        self.log_path = self.data_attempt / "runtime" / "database-server.log"
        self.datadir = self.data_attempt / "database"
        self.runtime_dir = self.data_attempt / "runtime"
        self.transaction_log_dir = self.log_attempt / "transaction-logs"
        self.socket_path = self.runtime_dir / "database.sock"
        self.db_user = "hdb_" + secrets.token_hex(6)
        self.db_password = secrets.token_hex(32)
        self.admin_password = secrets.token_hex(32)
        self.pgpass_path = self.data_attempt / "pgpass"
        self.mysql_defaults_path = self.data_attempt / "client.cnf"
        self.mysql_server_defaults_path = self.runtime_dir / "server.cnf"
        self.environment = os.environ.copy()
        for key in tuple(self.environment):
            if key.startswith("PG") or key.startswith("MYSQL_"):
                self.environment.pop(key, None)
        database_tmp = self.runtime_dir / "tmp"
        self.environment["TMP"] = str(database_tmp)
        self.environment["TMPDIR"] = self.environment["TMP"]
        self.environment["TEMP"] = self.environment["TMP"]
        self.secrets = [self.db_password, self.admin_password]
        self.version = None
        self.package_versions = None
        self.service_user = "mysql" if backend == "mariadb" else "postgres"
        self.service_account = require_service_account(self.service_user)

    def _database_version(self):
        executable = database_version_command(self.backend)
        return run_command(executable, 10, self.backend + " server version").strip()

    def _package_versions(self):
        packages = ["mariadb-server", "mariadb-connector-c"] if self.backend == "mariadb" else [
            "postgresql-server",
            "postgresql",
            "libpq",
        ]
        try:
            return run_command(["rpm", "-q", *packages], 10, "database RPM provenance").splitlines()
        except RuntimeError:
            # Debian-like development hosts can still expose the binaries, but
            # published Workshop support is limited to the declared Alma 9 image.
            return ["package query unavailable"]

    def start(self):
        if os.geteuid() != 0:
            raise RuntimeError("the local database lifecycle requires a root workload engine")
        if not port_is_available(self.port):
            raise RuntimeError(
                "engine-id database port %d is already in use; refusing to attach to another service"
                % self.port
            )
        self.datadir.mkdir(mode=0o700)
        self.runtime_dir.mkdir(mode=0o700)
        self.transaction_log_dir.mkdir(mode=0o700)
        chown_private_tree(self.datadir, self.service_account)
        chown_private_tree(self.runtime_dir, self.service_account)
        chown_private_tree(self.transaction_log_dir, self.service_account)
        database_tmp = self.runtime_dir / "tmp"
        database_tmp.mkdir(mode=0o700)
        chown_private_tree(database_tmp, self.service_account)
        self._check_service_user_can_traverse(self.datadir)
        self._check_service_user_can_traverse(self.runtime_dir)
        self._check_service_user_can_traverse(self.transaction_log_dir)

        if self.backend == "mariadb":
            self._start_mariadb()
        else:
            self._start_postgresql()
        self.version = self._database_version()
        self.package_versions = self._package_versions()
        self._wait_until_ready()
        self._verify_transaction_log_location()
        self._create_benchmark_login()
        self._wait_for_benchmark_login()
        return {
            "backend": self.backend,
            "version": self.version,
            "package_versions": self.package_versions,
            "port": self.port,
            "data_directory": str(self.datadir),
            "transaction_log_directory": str(self.transaction_log_dir),
            "socket_directory": str(self.runtime_dir),
            "process": process_is_our_database(self.process, self.backend, self.datadir, self.port),
            "memory_configuration": (
                {"innodb_buffer_pool_size": "128M", "max_connections": self.virtual_users + 8}
                if self.backend == "mariadb"
                else {
                    "shared_buffers": "128MB",
                    "work_mem": "4MB",
                    "max_connections": self.virtual_users + 8,
                }
            ),
        }

    def _check_service_user_can_traverse(self, directory):
        directory = Path(directory).resolve(strict=True)
        for ancestor in reversed((directory, *directory.parents)):
            command = [
                require_executable("runuser"),
                "-u",
                self.service_user,
                "--",
                require_executable("test"),
                "-x",
                str(ancestor),
            ]
            run_command(command, 10, "%s storage traversal preflight" % self.service_user)

    def _start_owned_daemon(self, command):
        self.log_stream = self.log_path.open("ab", buffering=0)
        os.chmod(self.log_path, 0o600)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self.log_stream,
            stderr=subprocess.STDOUT,
            cwd=str(self.data_attempt),
            env=self.environment,
            start_new_session=True,
            close_fds=True,
            user=self.service_account.pw_uid,
            group=self.service_account.pw_gid,
            extra_groups=[],
        )
        _register_process(self.process)
        return self.process

    def _start_mariadb(self):
        installer = require_executable("mariadb-install-db")
        daemon = require_executable("mariadbd")
        if len(os.fsencode(self.socket_path)) > 100:
            raise RuntimeError("MariaDB socket path is too long; select a shorter data mount path")
        self.mysql_server_defaults_path.write_text(
            '[mysqld]\ninnodb_log_group_home_dir="%s"\ninnodb_log_file_size=64M\n'
            % self.transaction_log_dir,
            encoding="utf-8",
        )
        os.chown(
            self.mysql_server_defaults_path,
            self.service_account.pw_uid,
            self.service_account.pw_gid,
        )
        os.chmod(self.mysql_server_defaults_path, 0o600)
        init_command = [
            str(installer),
            "--defaults-file=%s" % self.mysql_server_defaults_path,
            "--datadir=%s" % self.datadir,
            "--auth-root-authentication-method=normal",
            "--skip-test-db",
        ]
        run_command(
            init_command,
            MAX_DATABASE_START_SECONDS,
            "MariaDB private datadir initialization",
            env=self.environment,
            cwd=self.data_attempt,
            user_account=self.service_account,
        )
        max_connections = self.virtual_users + 8
        command = [
            str(daemon),
            "--defaults-file=%s" % self.mysql_server_defaults_path,
            "--console",
            "--skip-name-resolve",
            "--bind-address=127.0.0.1",
            "--port=%d" % self.port,
            "--max-connections=%d" % max_connections,
            "--innodb-buffer-pool-size=128M",
            "--tmp-table-size=16M",
            "--max-heap-table-size=16M",
            "--datadir=%s" % self.datadir,
            "--socket=%s" % self.socket_path,
            "--pid-file=%s" % (self.runtime_dir / "mariadbd.pid"),
            "--tmpdir=%s" % (self.runtime_dir / "tmp"),
        ]
        self._start_owned_daemon(command)

    def _start_postgresql(self):
        initdb = require_executable("initdb")
        postgres = require_executable("postgres")
        expected_socket = self.runtime_dir / (".s.PGSQL.%d" % self.port)
        if len(os.fsencode(expected_socket)) > 100:
            raise RuntimeError("PostgreSQL socket path is too long; select a shorter data mount path")
        init_command = [
            str(initdb),
            "-D",
            str(self.datadir),
            "--waldir=%s" % self.transaction_log_dir,
            "--username=postgres",
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
            "--encoding=UTF8",
            "--no-locale",
        ]
        run_command(
            init_command,
            MAX_DATABASE_START_SECONDS,
            "PostgreSQL private cluster initialization",
            env=self.environment,
            cwd=self.data_attempt,
            user_account=self.service_account,
        )
        pg_wal_link = self.datadir / "pg_wal"
        if not pg_wal_link.is_symlink() or pg_wal_link.resolve(strict=True) != self.transaction_log_dir.resolve(strict=True):
            raise RuntimeError("PostgreSQL initdb did not place pg_wal in the selected transaction-log directory")
        command = [
            str(postgres),
            "-D",
            str(self.datadir),
            "-h",
            "127.0.0.1",
            "-p",
            str(self.port),
            "-k",
            str(self.runtime_dir),
            "-c",
            "listen_addresses=127.0.0.1",
            "-c",
            "shared_buffers=128MB",
            "-c",
            "work_mem=4MB",
            "-c",
            "maintenance_work_mem=64MB",
            "-c",
            "max_wal_size=1GB",
            "-c",
            "max_connections=%d" % (self.virtual_users + 8),
            "-c",
            "unix_socket_permissions=0700",
            "-c",
            "logging_collector=off",
            "-c",
            "log_min_messages=warning",
            "-c",
            "fsync=on",
            "-c",
            "synchronous_commit=on",
        ]
        self._start_owned_daemon(command)

    def _wait_until_ready(self):
        deadline = time.monotonic() + MAX_DATABASE_START_SECONDS
        while time.monotonic() < deadline:
            raise_if_interrupted()
            return_code = self.process.poll()
            if return_code is not None:
                raise RuntimeError(
                    "%s daemon exited during startup with return code %s; see its owned log"
                    % (self.backend, return_code)
                )
            process_is_our_database(self.process, self.backend, self.datadir, self.port)
            try:
                if self.backend == "mariadb":
                    command = [
                        require_executable("mariadb-admin"),
                        "--no-defaults",
                        "--connect-timeout=2",
                        "--protocol=socket",
                        "--socket=%s" % self.socket_path,
                        "--user=root",
                        "ping",
                    ]
                    run_command(
                        command,
                        3,
                        "MariaDB owned-socket readiness check",
                        env=self.environment,
                    )
                else:
                    command = [
                        require_executable("pg_isready"),
                        "--host=127.0.0.1",
                        "--port=%d" % self.port,
                        "--username=postgres",
                        "--dbname=postgres",
                    ]
                    run_command(
                        command,
                        3,
                        "PostgreSQL TCP readiness check",
                        env=self.environment,
                    )
                return
            except RuntimeError:
                time.sleep(1)
        raise RunTimedOut(
            "%s did not become ready within %d seconds"
            % (self.backend, MAX_DATABASE_START_SECONDS)
        )

    def _create_benchmark_login(self):
        if self.backend == "mariadb":
            user = sql_literal(self.db_user)
            password = sql_literal(self.db_password)
            sql = (
                "CREATE USER %s@'127.0.0.1' IDENTIFIED BY %s;\n"
                "GRANT ALL PRIVILEGES ON *.* TO %s@'127.0.0.1' WITH GRANT OPTION;\n"
                "FLUSH PRIVILEGES;\n" % (user, password, user)
            )
            command = [
                require_executable("mariadb"),
                "--no-defaults",
                "--protocol=socket",
                "--socket=%s" % self.socket_path,
                "--user=root",
                "--batch",
                "--show-warnings",
            ]
            run_command(
                command,
                30,
                "MariaDB benchmark login creation",
                input_text=sql,
                env=self.environment,
                secrets_to_redact=self.secrets,
            )
            self.mysql_defaults_path.write_text(
                "[client]\nuser=%s\npassword=%s\nhost=127.0.0.1\nport=%d\nprotocol=tcp\n"
                % (self.db_user, self.db_password, self.port),
                encoding="utf-8",
            )
            os.chmod(self.mysql_defaults_path, 0o600)
        else:
            self.pgpass_path.write_text(
                "127.0.0.1:%d:postgres:postgres:%s\n"
                "127.0.0.1:%d:tpcc:postgres:%s\n"
                "127.0.0.1:%d:postgres:%s:%s\n"
                "127.0.0.1:%d:tpcc:%s:%s\n"
                % (
                    self.port,
                    self.admin_password,
                    self.port,
                    self.admin_password,
                    self.port,
                    self.db_user,
                    self.db_password,
                    self.port,
                    self.db_user,
                    self.db_password,
                ),
                encoding="utf-8",
            )
            os.chmod(self.pgpass_path, 0o600)
            sql = (
                "ALTER USER postgres WITH PASSWORD '%s';\n"
                "CREATE ROLE %s LOGIN PASSWORD '%s';\n"
                % (self.admin_password, self.db_user, self.db_password)
            )
            command = [
                require_executable("psql"),
                "--no-psqlrc",
                "--host=%s" % self.runtime_dir,
                "--port=%d" % self.port,
                "--username=postgres",
                "--dbname=postgres",
                "--set=ON_ERROR_STOP=1",
            ]
            run_command(
                command,
                30,
                "PostgreSQL benchmark login creation",
                input_text=sql,
                env=self.environment,
                secrets_to_redact=self.secrets,
                user_account=self.service_account,
            )

    def _wait_for_benchmark_login(self):
        if self.backend == "mariadb":
            command = [
                require_executable("mariadb"),
                "--defaults-file=%s" % self.mysql_defaults_path,
                "--batch",
                "--skip-column-names",
                "--execute=SELECT 1",
            ]
            output = run_command(
                command,
                10,
                "MariaDB benchmark TCP login",
                env=self.environment,
                secrets_to_redact=self.secrets,
            )
        else:
            env = self.environment.copy()
            env["PGPASSFILE"] = str(self.pgpass_path)
            command = [
                require_executable("psql"),
                "--no-psqlrc",
                "--host=127.0.0.1",
                "--port=%d" % self.port,
                "--username=%s" % self.db_user,
                "--dbname=postgres",
                "--tuples-only",
                "--no-align",
                "--command=SELECT 1",
            ]
            output = run_command(
                command,
                10,
                "PostgreSQL benchmark TCP login",
                env=env,
                secrets_to_redact=self.secrets,
            )
        if output.strip() != "1":
            raise RuntimeError("%s benchmark TCP login did not return SELECT 1" % self.backend)

    def verify_warehouse_count(self, expected):
        if self.backend == "mariadb":
            command = [
                require_executable("mariadb"),
                "--defaults-file=%s" % self.mysql_defaults_path,
                "--batch",
                "--skip-column-names",
                "--execute=SELECT COUNT(*) FROM tpcc.warehouse",
            ]
            env = self.environment
        else:
            env = self.environment.copy()
            env["PGPASSFILE"] = str(self.pgpass_path)
            command = [
                require_executable("psql"),
                "--no-psqlrc",
                "--host=127.0.0.1",
                "--port=%d" % self.port,
                "--username=postgres",
                "--dbname=tpcc",
                "--tuples-only",
                "--no-align",
                "--command=SELECT COUNT(*) FROM warehouse",
            ]
        output = run_command(
            command,
            30,
            "%s built warehouse-count check" % self.backend,
            env=env,
            secrets_to_redact=self.secrets,
        ).strip()
        if not output.isdecimal() or int(output) != expected:
            raise RuntimeError(
                "%s built %s warehouse rows, expected %d"
                % (self.backend, output or "no", expected)
            )
        return int(output)

    def _verify_transaction_log_location(self):
        if self.backend == "mariadb":
            command = [
                require_executable("mariadb"),
                "--no-defaults",
                "--protocol=socket",
                "--socket=%s" % self.socket_path,
                "--user=root",
                "--batch",
                "--skip-column-names",
                "--execute=SELECT @@innodb_log_group_home_dir",
            ]
            actual = run_command(
                command, 10, "MariaDB redo location check", env=self.environment
            ).strip()
            if Path(actual).resolve(strict=True) != self.transaction_log_dir.resolve(strict=True):
                raise RuntimeError("MariaDB is not using the selected transaction-log directory")
            redo_files = list(self.transaction_log_dir.glob("ib_logfile*"))
            if not redo_files or any(not path.is_file() for path in redo_files):
                raise RuntimeError("MariaDB did not create redo files in the selected transaction-log directory")
        else:
            pg_wal_link = self.datadir / "pg_wal"
            if not pg_wal_link.is_symlink() or pg_wal_link.resolve(strict=True) != self.transaction_log_dir.resolve(strict=True):
                raise RuntimeError("PostgreSQL is not using the selected transaction-log directory")
            if not any(path.is_file() for path in self.transaction_log_dir.iterdir()):
                raise RuntimeError("PostgreSQL did not create WAL files in the selected transaction-log directory")
        return str(self.transaction_log_dir)

    def hammerdb_parameters(self):
        if self.backend == "mariadb":
            return {
                "connection": {
                    "maria_host": "127.0.0.1",
                    "maria_port": str(self.port),
                    "maria_socket": "null",
                },
                "tpcc": {
                    "maria_user": self.db_user,
                    "maria_pass": self.db_password,
                    "maria_dbase": "tpcc",
                    "maria_storage_engine": "innodb",
                    "maria_partition": "false",
                },
            }
        return {
            "connection": {
                "pg_host": "127.0.0.1",
                "pg_port": str(self.port),
                "pg_sslmode": "disable",
            },
            "tpcc": {
                "pg_superuser": "postgres",
                "pg_superuserpass": self.admin_password,
                "pg_defaultdbase": "postgres",
                "pg_user": self.db_user,
                "pg_pass": self.db_password,
                "pg_dbase": "tpcc",
                "pg_tspace": "pg_default",
                "pg_storedprocs": "true",
                "pg_partition": "false",
            },
        }

    def _write_pgpass_for_hammerdb(self):
        if self.backend == "postgresql":
            self.pgpass_path.write_text(
                "127.0.0.1:%d:postgres:postgres:%s\n"
                "127.0.0.1:%d:tpcc:postgres:%s\n"
                "127.0.0.1:%d:postgres:%s:%s\n"
                "127.0.0.1:%d:tpcc:%s:%s\n"
                % (
                    self.port,
                    self.admin_password,
                    self.port,
                    self.admin_password,
                    self.port,
                    self.db_user,
                    self.db_password,
                    self.port,
                    self.db_user,
                    self.db_password,
                ),
                encoding="utf-8",
            )
            os.chmod(self.pgpass_path, 0o600)

    def hammerdb_environment(self):
        self._write_pgpass_for_hammerdb()
        if self.backend == "postgresql":
            self.environment["PGPASSFILE"] = str(self.pgpass_path)
        return self.environment.copy()

    def cleanup(self):
        if self.process is not None:
            try:
                # The direct leader may have exited while owned descendants
                # still hold the database files, so always supervise its PGID.
                terminate_process_group(self.process, DATABASE_SHUTDOWN_SECONDS)
                if not process_group_is_stopped(self.process.pid):
                    raise RuntimeError("owned database process group did not stop cleanly")
                marker = process_marker(self.process)
                self.process.wait()
                if marker.get("alive"):
                    raise RuntimeError("owned database process did not stop cleanly")
                _unregister_process(self.process)
                marker["group_stopped"] = True
                return marker
            finally:
                if self.log_stream is not None:
                    self.log_stream.flush()
                    self.log_stream.close()
                    self.log_stream = None
        if self.log_stream is not None:
            self.log_stream.close()
            self.log_stream = None
        return {"alive": False, "pid": None, "group_stopped": True}


def hammerdb_script(backend, parameters, mode, args):
    """Build a small native Tcl script with no shell interpolation."""
    database = "maria" if backend == "mariadb" else "pg"
    lines = ["dbset db %s" % database, "dbset bm TPC-C"]
    for category, settings in parameters.items():
        for key, value in settings.items():
            lines.append("diset %s %s %s" % (category, key, tcl_braced(value)))
    if mode == "driver-check":
        package = "mariatcl" if backend == "mariadb" else "Pgtcl"
        lines.extend(
            [
                "if {[catch {package require %s} driver_version]} {puts stderr \"HAMMERDB_DRIVER_ERROR: %s package load failed: $driver_version\"; exit 1}"
                % (package, package),
                "puts \"HAMMERDB_NATIVE_DRIVER_PACKAGE %s $driver_version\"" % package,
                "if {[catch {loadscript} result]} {puts stderr \"HAMMERDB_DRIVER_ERROR: $result\"; exit 1}",
                "puts \"HAMMERDB_DRIVER_READY\"",
                "exit",
            ]
        )
    elif mode == "schema-build":
        prefix = "maria" if backend == "mariadb" else "pg"
        lines.extend(
            [
                "diset tpcc %s_count_ware %d" % (prefix, args.warehouses),
                "diset tpcc %s_num_vu 1" % prefix,
                "puts \"HAMMERDB_SCHEMA_BUILD_BEGIN\"",
                "if {[catch {buildschema} result]} {puts stderr \"HAMMERDB_SCHEMA_BUILD_ERROR: $result\"; exit 1}",
                "puts \"HAMMERDB_SCHEMA_BUILD_COMPLETED\"",
                "exit",
            ]
        )
    elif mode == "schema-check":
        lines.extend(
            [
                "puts \"HAMMERDB_SCHEMA_CHECK_BEGIN\"",
                "if {[catch {checkschema} result]} {puts stderr \"HAMMERDB_SCHEMA_CHECK_ERROR: $result\"; exit 1}",
                "puts \"HAMMERDB_SCHEMA_CHECK_COMPLETED\"",
                "exit",
            ]
        )
    elif mode == "timed-run":
        prefix = "maria" if backend == "mariadb" else "pg"
        lines.extend(
            [
                "diset tpcc %s_driver timed" % prefix,
                "diset tpcc %s_raiseerror true" % prefix,
                "diset tpcc %s_total_iterations %d" % (prefix, MAX_TOTAL_ITERATIONS),
                "diset tpcc %s_rampup %d" % (prefix, args.rampup_minutes),
                "diset tpcc %s_duration %d" % (prefix, args.duration_minutes),
                "diset tpcc %s_allwarehouse true" % prefix,
                "puts \"HAMMERDB_LOADSCRIPT_BEGIN\"",
                "if {[catch {loadscript} result]} {puts stderr \"HAMMERDB_LOADSCRIPT_ERROR: $result\"; exit 1}",
                "vuset vu %d" % args.virtual_users,
                "if {[catch {vucreate} result]} {puts stderr \"HAMMERDB_VUCREATE_ERROR: $result\"; exit 1}",
                "puts \"HAMMERDB_VURUN_BEGIN\"",
                "if {[catch {vurun} result]} {puts stderr \"HAMMERDB_VURUN_ERROR: $result\"; catch {vudestroy}; exit 1}",
                "puts \"HAMMERDB_VURUN_RETURNED\"",
                "if {[catch {vudestroy} result]} {puts stderr \"HAMMERDB_VUDESTROY_ERROR: $result\"; exit 1}",
                "puts \"HAMMERDB_VUDESTROY_COMPLETED\"",
                "exit",
            ]
        )
    else:
        raise ValueError("unknown HammerDB script mode: %s" % mode)
    return "\n".join(lines) + "\n"


def validate_schema_output(mode, output, backend):
    try:
        validate_native_schema_output(backend, mode, output)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def run_hammerdb(args, database, tmpdir, secrets_to_redact, mode):
    """Run HammerDB while recording receipt times for the native monitor markers."""
    raise_if_interrupted()
    script_path = Path(tmpdir) / ("%s.tcl" % mode)
    script_path.write_text(hammerdb_script(database.backend, database.hammerdb_parameters(), mode, args))
    os.chmod(script_path, 0o600)
    cli = HAMMERDB_CLI
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise RuntimeError("prepared HammerDB v6.0 CLI is missing: %s" % cli)

    environment = database.hammerdb_environment()
    environment["TMP"] = str(tmpdir)
    environment["TMPDIR"] = str(tmpdir)
    environment["TEMP"] = str(tmpdir)
    environment["HOME"] = str(tmpdir)
    output_path = Path(tmpdir) / (mode + "-raw.txt")
    completed_lines = []
    event_times = {}
    messages = queue.Queue(maxsize=256)
    stop_reader = threading.Event()
    command = [str(cli), "tcl", "auto", str(script_path)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(HAMMERDB_HOME),
        env=environment,
        start_new_session=True,
        close_fds=True,
    )
    _register_process(process)

    def consume_output():
        try:
            for line in process.stdout:
                item = (time.time_ns(), time.monotonic_ns(), line)
                while not stop_reader.is_set():
                    try:
                        messages.put(item, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                else:
                    break
        except (OSError, ValueError):
            pass
        finally:
            if not stop_reader.is_set():
                try:
                    messages.put((None, None, None), timeout=0.5)
                except queue.Full:
                    pass

    reader = threading.Thread(target=consume_output, name="hammerdb-output", daemon=True)
    reader.start()
    timeout = {
        "driver-check": 120,
        "schema-build": MAX_SCHEMA_BUILD_SECONDS,
        "schema-check": MAX_SCHEMA_CHECK_SECONDS,
        "timed-run": (
            args.rampup_minutes * 60
            + args.duration_minutes * 60
            + MAX_HAMMERDB_CLEANUP_SECONDS
            + 120
        ),
    }[mode]
    deadline = time.monotonic() + timeout
    drain_deadline = None
    reached_eof = False
    timed_out = False
    interrupted = False
    output_exceeded = False
    output_bytes = 0
    try:
        while True:
            if SIGNAL_RECEIVED is not None and drain_deadline is None:
                interrupted = True
                terminate_process_group(process)
                drain_deadline = time.monotonic() + 3
            now = time.monotonic()
            if now >= deadline and drain_deadline is None:
                timed_out = True
                terminate_process_group(process)
                drain_deadline = time.monotonic() + 3
            if reached_eof and process.poll() is not None:
                break
            if drain_deadline is not None and now >= drain_deadline:
                break
            if reached_eof:
                time.sleep(0.05)
                continue
            try:
                epoch_ns, monotonic_ns, line = messages.get(timeout=0.2)
            except queue.Empty:
                if not reader.is_alive():
                    reached_eof = True
                continue
            if epoch_ns is None:
                reached_eof = True
                continue
            if output_exceeded:
                continue
            output_bytes += len(line.encode("utf-8", "replace"))
            if output_bytes > MAX_STAGE_OUTPUT_BYTES:
                output_exceeded = True
                terminate_process_group(process)
                drain_deadline = time.monotonic() + 3
                continue
            clean_line = redact_secrets(line, secrets_to_redact)
            completed_lines.append(clean_line)
            if mode == "timed-run":
                if "Vuser 1:Rampup complete, Taking start Transaction Count." in line:
                    event_times.setdefault("measurement_start_epoch_ns", epoch_ns)
                    event_times.setdefault("measurement_start_monotonic_ns", monotonic_ns)
                if "Vuser 1:Test complete, Taking end Transaction Count." in line:
                    event_times.setdefault("measurement_end_epoch_ns", epoch_ns)
                    event_times.setdefault("measurement_end_monotonic_ns", monotonic_ns)
        reader.join(timeout=1)
        if process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_process_group(process)
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process_group(process)
        if process.poll() is None:
            terminate_process_group(process)
        process.wait(timeout=2)
        output = "".join(completed_lines)
        write_private_text(output_path, output)
        if output_exceeded:
            raise RuntimeError(
                "HammerDB %s output exceeded its %d MiB diagnostic limit"
                % (mode, MAX_STAGE_OUTPUT_BYTES // (1024 * 1024))
            )
        if timed_out:
            raise RunTimedOut("HammerDB %s exceeded its %d second limit" % (mode, timeout))
        if interrupted or SIGNAL_RECEIVED is not None:
            raise_if_interrupted()
        if process.returncode != 0:
            raise RuntimeError(
                "HammerDB %s exited with status %d: %s"
                % (mode, process.returncode, output[-4000:].strip())
            )
        markers = {
            "driver-check": "HAMMERDB_DRIVER_READY",
            "schema-build": "HAMMERDB_SCHEMA_BUILD_COMPLETED",
            "schema-check": "HAMMERDB_SCHEMA_CHECK_COMPLETED",
            "timed-run": "HAMMERDB_VUDESTROY_COMPLETED",
        }
        if markers[mode] not in output:
            raise RuntimeError("HammerDB %s did not report its completion marker" % mode)
        if mode in ("schema-build", "schema-check"):
            validate_schema_output(mode, output, database.backend)
        return {
            "output": output,
            "output_path": str(output_path),
            "return_code": process.returncode,
            "event_times": event_times,
            "process": process_marker(process),
        }
    finally:
        try:
            terminate_process_group(process)
        finally:
            stop_reader.set()
            if process.stdout is not None and reader.is_alive():
                try:
                    os.close(process.stdout.fileno())
                except OSError:
                    pass
                reader.join(timeout=2)
            elif process.stdout is not None:
                process.stdout.close()
            if process_group_is_stopped(process.pid):
                _unregister_process(process)


def _copy_artifact(source, destination, secrets_to_redact=()):
    source = Path(source)
    destination = Path(destination)
    text = source.read_text(encoding="utf-8", errors="replace")
    write_private_text(destination, redact_secrets(text, secrets_to_redact))


def _capture_database_log(database, output_root):
    if database.log_stream is not None:
        database.log_stream.flush()
        database.log_stream.close()
        database.log_stream = None
    if database.log_path.exists():
        _copy_artifact(database.log_path, Path(output_root) / DATABASE_LOG_NAME, database.secrets)
    else:
        write_private_text(Path(output_root) / DATABASE_LOG_NAME, "")


def _delete_owned_data_attempt(path, run_token):
    path = Path(path)
    if not path.exists():
        return True
    marker_path = path / "owner.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("refusing to remove an attempt directory without a readable ownership marker") from exc
    if marker.get("run_token") != run_token or marker.get("parent_pid") != os.getpid():
        raise RuntimeError("refusing to remove a data directory not owned by this adapter process")
    resolved = path.resolve(strict=True)
    if not resolved.name.startswith(DATA_ATTEMPT_PREFIX):
        raise RuntimeError("refusing to remove a directory outside the HammerDB data prefix")
    shutil.rmtree(resolved)
    return True


def _delete_owned_log_attempt(path, run_token, expected_parent):
    path = Path(path)
    if not path.exists():
        return True
    marker_path = path / "owner.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("refusing to remove a transaction-log directory without an ownership marker") from exc
    if marker.get("run_token") != run_token or marker.get("parent_pid") != os.getpid():
        raise RuntimeError("refusing to remove a transaction-log directory not owned by this adapter process")
    resolved = path.resolve(strict=True)
    if not resolved.name.startswith(LOG_ATTEMPT_PREFIX) or resolved.parent != Path(expected_parent).resolve(strict=True):
        raise RuntimeError("refusing to remove a transaction-log directory outside its selected root")
    shutil.rmtree(resolved)
    return True


def _delete_owned_mechanics_root(path, run_token):
    if path is None or not Path(path).exists():
        return True
    path = Path(path)
    marker_path = path / "owner.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("refusing to remove a mechanics scratch root without an ownership marker") from exc
    if marker.get("run_token") != run_token or marker.get("parent_pid") != os.getpid():
        raise RuntimeError("refusing to remove a mechanics scratch root not owned by this adapter process")
    resolved = path.resolve(strict=True)
    if not resolved.name.startswith("hammerdb-mechanics-") or resolved.parent != Path("/tmp").resolve():
        raise RuntimeError("refusing to remove a mechanics scratch root outside /tmp")
    shutil.rmtree(resolved)
    return True


def _scrub_private_state(path):
    """Remove credentials and HammerDB's private job/config store after its CLI exits."""
    path = Path(path)
    for name in ("pgpass", "client.cnf"):
        try:
            (path / name).unlink()
        except FileNotFoundError:
            pass
    hammerdb_tmp = path / "hammerdb-tmp"
    if hammerdb_tmp.exists():
        shutil.rmtree(hammerdb_tmp)
    return True


def _new_manifest(
    args,
    label,
    run_token,
    engine_id,
    port,
    data_root,
    log_root,
    data_storage,
    log_storage,
    resources,
):
    architecture = os.uname().machine
    return {
        "schema_version": 1,
        "status": "running",
        "benchmark": "hammerdb",
        "engine": {"label": label, "engine_id": engine_id},
        "upstream": {
            "hammerdb_version": HAMMERDB_VERSION,
            "hammerdb_commit": HAMMERDB_COMMIT,
            "archive_sha256": HAMMERDB_TARBALL_SHA256.get(architecture),
            "architecture": architecture,
            "license": "GNU GPL-3.0-or-later",
        },
        "requested": {
            "backend": args.backend,
            "workload": "TPROC-C",
            "warehouses": args.warehouses,
            "virtual_users": args.virtual_users,
            "monitor_virtual_users": 1,
            "total_virtual_users": args.virtual_users + 1,
            "rampup_minutes": args.rampup_minutes,
            "duration_minutes": args.duration_minutes,
            "data_dir_option": args.data_dir,
            "log_dir_option": args.log_dir,
            "data_root": str(data_root),
            "log_root": str(log_root),
            "storage_parity_claimed": False,
        },
        "storage": {
            "data": data_storage,
            "logs": log_storage,
            "policy": "private data and native transaction-log children per sample; caller-owned roots are never changed or removed",
            "sample_cwd_limit": "the mechanics-only dot option maps to an owned /tmp scratch root and does not establish storage parity",
        },
        "port": port,
        "resources": resources,
        "attempt": {
            "run_token": run_token,
            "started_epoch_ns": current_epoch_ns(),
            "runtime_bound_seconds": runtime_seconds(args),
        },
        "database": {},
        "native": {},
        "measurement": {},
        "cleanup": {
            "owned_database_stopped": False,
            "owned_process_groups_stopped": False,
            "owned_data_removed": False,
            "owned_transaction_logs_removed": False,
        },
    }


def _clear_stale_artifacts(cwd):
    """Remove prior attempt outputs before any preflight can fail."""
    for filename in ARTIFACT_FILES:
        path = Path(cwd) / filename
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _base_manifest():
    return {
        "schema_version": 1,
        "status": "invalid",
        "benchmark": "hammerdb",
        "upstream": {
            "hammerdb_version": HAMMERDB_VERSION,
            "hammerdb_commit": HAMMERDB_COMMIT,
            "license": "GNU GPL-3.0-or-later",
        },
        "attempt": {"started_epoch_ns": current_epoch_ns()},
        "native": {},
        "measurement": {},
        "cleanup": {
            "owned_database_stopped": False,
            "owned_process_groups_stopped": False,
            "owned_data_removed": False,
            "owned_transaction_logs_removed": False,
        },
    }


def _write_error(path, error, secrets_to_redact):
    message = redact_secrets(str(error), secrets_to_redact)
    write_private_text(path, message[:8000] + "\n")


def execute_sample(argv):
    global SIGNAL_RECEIVED
    cwd = Path.cwd().resolve(strict=True)
    manifest_path = cwd / MANIFEST_NAME
    _clear_stale_artifacts(cwd)
    manifest = _base_manifest()
    try:
        atomic_json(manifest_path, manifest)
    except OSError:
        # The final error path below still attempts to leave an invalid report.
        pass

    args = None
    label = os.environ.get("RS_CS_LABEL", "")
    engine_id = None
    port = None
    data_root = None
    log_root = None
    data_storage = None
    log_storage = None
    data_attempt = None
    log_attempt = None
    mechanics_root = None
    data_root_option = "."
    log_root_option = "."
    run_token = uuid.uuid4().hex
    tmpdir = None
    database = None
    secret_values = []
    failure = None
    success = False
    data_attempt_created = False
    log_attempt_created = False
    mechanics_root_created = False

    try:
        args = parse_cli(argv)
        if "HK_CPUS" in os.environ or "WORKLOAD_CPUS" in os.environ:
            raise ValueError(
                "CPU partitioning is unsupported by this initial client adapter; run with cpu-partitioning disabled"
            )
        engine_id = engine_id_from_label(label)
        port = database_port(engine_id, args.backend)
        data_root_option = args.data_dir
        log_root_option = args.log_dir
        if args.data_dir == "." or args.log_dir == ".":
            mechanics_root = Path(tempfile.mkdtemp(prefix="hammerdb-mechanics-", dir="/tmp"))
            mechanics_root_created = True
            os.chmod(mechanics_root, 0o711)
            atomic_json(
                mechanics_root / "owner.json",
                {"run_token": run_token, "parent_pid": os.getpid(), "engine_id": engine_id},
            )
        data_root = mechanics_root if args.data_dir == "." else storage_root(args.data_dir, cwd=cwd)
        log_root = mechanics_root if args.log_dir == "." else storage_root(args.log_dir, cwd=cwd)
        data_storage = mount_provenance(data_root)
        log_storage = mount_provenance(log_root)
        for name, value in (("data", data_storage), ("logs", log_storage)):
            selected = data_root_option if name == "data" else log_root_option
            value["scope"] = "mechanics-temp" if selected == "." else "explicit-engine-mount"
            value["parity_claimed"] = False
        if not HAMMERDB_HOME.is_dir() or not HAMMERDB_CLI.is_file():
            raise RuntimeError("HammerDB v6.0 is not installed at the pinned Workshop path")
        if not (HAMMERDB_HOME / "LICENSE").is_file():
            raise RuntimeError("the prepared HammerDB installation is missing its GPL license")
        if os.uname().machine not in HAMMERDB_TARBALL_SHA256:
            raise RuntimeError("the prepared HammerDB release is unsupported on this architecture")
        resources = validate_resources(args, data_root, log_root)
        resources["data_mount"] = data_storage
        resources["log_mount"] = log_storage
        try:
            resources["engine_allowed_cpus"] = sorted(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            resources["engine_allowed_cpus"] = None

        data_attempt = Path(
            tempfile.mkdtemp(
                prefix=DATA_ATTEMPT_PREFIX + str(engine_id) + "-",
                dir=data_root,
            )
        )
        data_attempt_created = True
        os.chmod(data_attempt, 0o711)
        atomic_json(
            data_attempt / "owner.json",
            {"run_token": run_token, "parent_pid": os.getpid(), "engine_id": engine_id},
        )
        log_attempt = Path(
            tempfile.mkdtemp(
                prefix=LOG_ATTEMPT_PREFIX + str(engine_id) + "-",
                dir=log_root,
            )
        )
        log_attempt_created = True
        os.chmod(log_attempt, 0o711)
        atomic_json(
            log_attempt / "owner.json",
            {"run_token": run_token, "parent_pid": os.getpid(), "engine_id": engine_id},
        )
        tmpdir = data_attempt / "hammerdb-tmp"
        tmpdir.mkdir(mode=0o700)

        manifest = _new_manifest(
            args,
            label,
            run_token,
            engine_id,
            port,
            data_root,
            log_root,
            data_storage,
            log_storage,
            resources,
        )
        atomic_json(manifest_path, manifest)
        database = LocalDatabase(args.backend, data_attempt, log_attempt, port, args.virtual_users)
        secret_values.extend(database.secrets)
        cli_version = run_command(
            [str(HAMMERDB_CLI), "--help"],
            20,
            "HammerDB CLI version check",
            env={
                **os.environ,
                "TMP": str(tmpdir),
                "TMPDIR": str(tmpdir),
                "TEMP": str(tmpdir),
                "HOME": str(tmpdir),
            },
            cwd=HAMMERDB_HOME,
        )
        if "HammerDB CLI v6.0" not in cli_version:
            raise RuntimeError("the installed HammerDB CLI does not identify itself as version 6.0")
        manifest["upstream"]["cli_version_check"] = "HammerDB CLI v6.0"

        database_info = database.start()
        manifest["database"] = database_info
        driver_check = run_hammerdb(args, database, tmpdir, secret_values, "driver-check")
        manifest["native"]["driver_check_return_code"] = driver_check["return_code"]

        build = run_hammerdb(args, database, tmpdir, secret_values, "schema-build")
        _copy_artifact(build["output_path"], cwd / "hammerdb-schema-build.txt", secret_values)
        manifest["database"]["schema_build_return_code"] = build["return_code"]
        manifest["database"]["schema_build_status"] = "native v6 TPROC-C schema marker and single schema VU succeeded"

        check = run_hammerdb(args, database, tmpdir, secret_values, "schema-check")
        _copy_artifact(check["output_path"], cwd / "hammerdb-schema-check.txt", secret_values)
        manifest["database"]["schema_check_return_code"] = check["return_code"]
        manifest["database"]["schema_check_status"] = "native TPROC-C schema and consistency checks succeeded"
        warehouse_count = database.verify_warehouse_count(args.warehouses)
        manifest["database"]["warehouse_count"] = warehouse_count
        manifest["database"]["schema_ready"] = True

        run = run_hammerdb(args, database, tmpdir, secret_values, "timed-run")
        _copy_artifact(run["output_path"], cwd / NATIVE_OUTPUT_NAME, secret_values)
        parsed = parse_native_output(
            run["output"],
            args.backend,
            args.warehouses,
            args.virtual_users,
            args.rampup_minutes,
            args.duration_minutes,
        )
        window = measurement_interval(
            run["event_times"].get("measurement_start_epoch_ns"),
            run["event_times"].get("measurement_end_epoch_ns"),
            args.duration_minutes,
            run["event_times"].get("measurement_start_monotonic_ns"),
            run["event_times"].get("measurement_end_monotonic_ns"),
        )
        manifest["native"].update(
            {
                "return_code": run["return_code"],
                "result": parsed,
                "worker_completion": "all configured workload VUs completed after measurement",
            }
        )
        manifest["measurement"].update(window)
        manifest["measurement"]["start_epoch_ns"] = run["event_times"]["measurement_start_epoch_ns"]
        manifest["measurement"]["end_epoch_ns"] = run["event_times"]["measurement_end_epoch_ns"]
        manifest["measurement"]["start_monotonic_ns"] = run["event_times"]["measurement_start_monotonic_ns"]
        manifest["measurement"]["end_monotonic_ns"] = run["event_times"]["measurement_end_monotonic_ns"]
        manifest["database"]["timed_run_return_code"] = run["return_code"]
        success = True
    except BaseException as exc:
        failure = exc
        if isinstance(exc, RunInterrupted):
            manifest["attempt"]["interrupted"] = True
        elif isinstance(exc, RunTimedOut):
            manifest["attempt"]["timed_out"] = True
        else:
            manifest["attempt"]["interrupted"] = False
            manifest["attempt"]["timed_out"] = False
    finally:
        cleanup_errors = []
        owned_processes = {process.pid: process for process in ACTIVE_PROCESSES}
        if database is not None and database.process is not None:
            owned_processes[database.process.pid] = database.process
        for process in list(owned_processes.values()):
            try:
                terminate_process_group(process)
                if not process_group_is_stopped(process.pid):
                    raise RuntimeError("owned process group %d is still live" % process.pid)
            except Exception as exc:
                cleanup_errors.append("failed to stop an owned subprocess: %s" % exc)
        if database is not None:
            try:
                database_cleanup = database.cleanup()
                if database_cleanup.get("group_stopped") is not True:
                    raise RuntimeError("owned database process group stop was not confirmed")
                manifest["cleanup"]["database_process"] = database_cleanup
                manifest["cleanup"]["owned_database_stopped"] = True
                if database.version:
                    manifest["database"]["version"] = database.version
                if database.package_versions:
                    manifest["database"]["package_versions"] = database.package_versions
            except Exception as exc:
                cleanup_errors.append("database cleanup failed: %s" % exc)
        else:
            # No local database object was created, so the adapter did not
            # start an owned daemon in this attempt.
            manifest["cleanup"]["owned_database_stopped"] = True
        all_process_groups_stopped = True
        for process in owned_processes.values():
            if process_group_has_live_members(process.pid):
                all_process_groups_stopped = False
                cleanup_errors.append(
                    "owned process group %d still has a live member" % process.pid
                )
        manifest["cleanup"]["owned_process_groups_stopped"] = all_process_groups_stopped
        if database is not None:
            try:
                _capture_database_log(database, cwd)
            except Exception as exc:
                cleanup_errors.append("database log capture failed: %s" % exc)
        if tmpdir is not None and Path(tmpdir).is_dir():
            for stage, destination_name in HAMMERDB_STAGE_ARTIFACTS.items():
                source = Path(tmpdir) / (stage + "-raw.txt")
                if source.is_file():
                    try:
                        _copy_artifact(
                            source,
                            cwd / destination_name,
                            secret_values,
                        )
                    except Exception as exc:
                        cleanup_errors.append("HammerDB %s diagnostic capture failed: %s" % (stage, exc))
        safe_to_remove_owned_storage = (
            all_process_groups_stopped
            and manifest["cleanup"].get("owned_database_stopped") is True
        )
        if data_attempt_created:
            try:
                if not safe_to_remove_owned_storage:
                    raise RuntimeError(
                        "refusing to remove database files while an owned process group may still use them"
                    )
                manifest["cleanup"]["private_credentials_removed"] = _scrub_private_state(data_attempt)
                manifest["cleanup"]["owned_data_removed"] = _delete_owned_data_attempt(
                    data_attempt, run_token
                )
            except Exception as exc:
                cleanup_errors.append("private data cleanup failed: %s" % exc)
        if log_attempt_created:
            try:
                if not safe_to_remove_owned_storage:
                    raise RuntimeError(
                        "refusing to remove transaction logs while an owned process group may still use them"
                    )
                manifest["cleanup"]["owned_transaction_logs_removed"] = _delete_owned_log_attempt(
                    log_attempt, run_token, log_root
                )
            except Exception as exc:
                cleanup_errors.append("transaction-log cleanup failed: %s" % exc)
        if mechanics_root_created:
            try:
                if (data_attempt_created and data_attempt.exists()) or (log_attempt_created and log_attempt.exists()):
                    raise RuntimeError("refusing to remove a mechanics scratch root while owned attempts remain")
                manifest["cleanup"]["mechanics_scratch_removed"] = _delete_owned_mechanics_root(
                    mechanics_root, run_token
                )
            except Exception as exc:
                cleanup_errors.append("mechanics scratch cleanup failed: %s" % exc)
        if SIGNAL_RECEIVED is not None and failure is None:
            try:
                raise_if_interrupted()
            except RunInterrupted as exc:
                failure = exc
                success = False

    if cleanup_errors:
        manifest.setdefault("cleanup", {})["errors"] = cleanup_errors
        if failure is None:
            failure = RuntimeError("; ".join(cleanup_errors))
        success = False

    manifest.setdefault("attempt", {})["completed_epoch_ns"] = current_epoch_ns()
    started = manifest["attempt"].get("started_epoch_ns")
    if isinstance(started, int):
        manifest["attempt"]["elapsed_seconds"] = (
            manifest["attempt"]["completed_epoch_ns"] - started
        ) / 1_000_000_000
    manifest["attempt"]["log_attempt_directory"] = str(log_attempt) if log_attempt else None
    manifest["attempt"]["mechanics_root"] = str(mechanics_root) if mechanics_root else None
    manifest["attempt"].pop("run_token", None)
    manifest.setdefault("cleanup", {})["data_attempt_directory"] = str(data_attempt) if data_attempt else None
    manifest["cleanup"]["database_process_stopped"] = manifest["cleanup"].get(
        "owned_database_stopped", False
    )

    if failure is not None or not success:
        manifest["status"] = "invalid"
        manifest.setdefault("attempt", {})["timed_out"] = isinstance(failure, RunTimedOut)
        manifest.setdefault("attempt", {})["interrupted"] = isinstance(failure, RunInterrupted)
        manifest["failure"] = {
            "type": type(failure).__name__ if failure is not None else "RunError",
            "message": redact_secrets(str(failure or "run did not reach the valid state"), secret_values),
        }
        try:
            _write_error(cwd / ERROR_NAME, manifest["failure"]["message"], secret_values)
        finally:
            try:
                atomic_json(manifest_path, manifest)
            except Exception:
                pass
        return 1

    try:
        raise_if_interrupted()
    except RunInterrupted as exc:
        manifest["status"] = "invalid"
        manifest["attempt"]["interrupted"] = True
        manifest["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_error(cwd / ERROR_NAME, manifest["failure"]["message"], secret_values)
        atomic_json(manifest_path, manifest)
        return 1

    manifest["status"] = "valid"
    manifest["attempt"]["timed_out"] = False
    manifest["attempt"]["interrupted"] = False
    manifest["attempt"]["return_code"] = 0
    manifest["cleanup"]["completed"] = True
    try:
        atomic_json(manifest_path, manifest)
    except Exception as exc:
        manifest["status"] = "invalid"
        manifest["failure"] = {"type": type(exc).__name__, "message": "could not write final manifest"}
        _write_error(cwd / ERROR_NAME, manifest["failure"]["message"], secret_values)
        try:
            atomic_json(manifest_path, manifest)
        except Exception:
            pass
        return 1
    if SIGNAL_RECEIVED is not None:
        manifest["status"] = "invalid"
        manifest["attempt"]["interrupted"] = True
        manifest["attempt"]["return_code"] = 1
        manifest["cleanup"]["completed"] = False
        manifest["failure"] = {"type": "RunInterrupted", "message": "run interrupted before output was reported"}
        _write_error(cwd / ERROR_NAME, manifest["failure"]["message"], secret_values)
        atomic_json(manifest_path, manifest)
        return 1
    print(
        "HammerDB TPROC-C completed: %s NOPM, %s %s TPM"
        % (
            manifest["native"]["result"]["nopm"],
            manifest["native"]["result"]["tpm"],
            manifest["native"]["result"]["tpm_backend"],
        )
    )
    return 0


def main(argv):
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        return execute_sample(argv)
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc)
        try:
            write_private_text(Path(ERROR_NAME), message + "\n")
            manifest = _base_manifest()
            manifest["failure"] = {"type": type(exc).__name__, "message": message[:8000]}
            atomic_json(Path(MANIFEST_NAME), manifest)
        except OSError:
            pass
        print("HammerDB run failed: %s" % message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
