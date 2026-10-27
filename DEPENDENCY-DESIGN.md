# HammerDB dependency and execution design

## Upstream and license

- The default userenv profile uses the official HammerDB v6.0 RHEL 9 release assets. The v6.0 tag resolves to commit `d33f879aec858063edd17aa2daa46db03abb2bae`.
- The x86_64 archive SHA-256 is `b4e0b1c84f9ac74802c86628b3ebca527ef314404be68827fca30174147a92b6`; the aarch64 archive SHA-256 is `6f6687ff3f9545dee2a1d2844a236a6df71a446621f750dad66dfd828ea5a4c9`.
- AlmaLinux 8 and RHUI 8 use HammerDB v5.0's official RHEL 8 x86_64 release asset. Its tag resolves to commit `22ded31bf47b2ffe25263d686c0fc630965cfb76`; the archive's published SHA-1 is `f2d6a8026fa3ed6e3e427d30b79ec738abd53848` and the independently computed SHA-256 pin is `e547d11faaeec54b721cd661683a020c241f4474f11ece8803564cb17abd86d3`.
- The release split is an EL8 compatibility boundary: HammerDB 6.0's RHEL 9 binary requires GLIBC versions unavailable on EL8. Workshop verifies the selected archive checksums, extracts each version to its versioned directory, checks the CLI and `LICENSE`, and updates `/opt/crucible/software/HammerDB` to the selected version. The adapter validates each sample manifest against either pinned release, independent of the controller's default version. HammerDB itself is neither rebuilt nor patched.
- The upstream distribution is GPL-3.0; the complete upstream license is retained in `third-party/HammerDB-GPL-3.0.txt`. Adapter files use Apache-2.0 in `LICENSE`.
- Each prepared userenv supplies MariaDB and PostgreSQL server/client packages. The client manifest records the exact HammerDB release, database binary versions, and installed RPM versions used for each sample.

## Database lifecycle

- The selected backend starts in the same client engine as HammerDB. No server role or external database is used.
- Each attempt creates a random private database child below `--data-dir` and a private transaction-log child below `--log-dir`. Only these children are created; caller-owned roots are never chowned, formatted, or removed. The mechanics-only dot default maps to a fresh adapter-owned `/tmp` root because Rickshaw's mode-0700 sample directory is not traversable by the database UIDs.
- MariaDB and PostgreSQL execute directly as their provisioned `mysql` and `postgres` UIDs in owned process groups. The adapter creates a fresh cluster, binds TCP only to `127.0.0.1` on a deterministic engine-specific port, confirms process identity and readiness, and stops the owned process group before deleting private database files.
- Sequential runs may leave completed TCP connections in `TIME_WAIT` after the daemon and owned files are gone. The port preflight enables `SO_REUSEADDR` and calls both `bind` and `listen`: this permits reusing a port in `TIME_WAIT` while rejecting a live listener. The check does not reserve the port, so a daemon bind race still fails startup; there is no fallback connection to another service.
- MariaDB uses a private option file to set `innodb_log_group_home_dir`; PostgreSQL uses `initdb --waldir`. The adapter verifies MariaDB's live variable and redo files, and PostgreSQL's `pg_wal` link and files, against the selected `--log-dir` child. Both database and transaction-log children are removed after the owned process stops. Sanitized HammerDB and database diagnostics are retained separately in the sample directory and archived by Rickshaw.
- A private HammerDB TMP/config/job-state directory is created before the CLI starts. The driver check explicitly requires the packaged `mariatcl` or `Pgtcl` Tcl package before generating a driver script. Passwords are generated per attempt, stored only in private files while needed, removed with the data child, and redacted from retained diagnostics.

## Measurement and validity

- Each sample builds and checks a new schema, then uses HammerDB's timed TPROC-C driver. HammerDB native build/check success records and an SQL warehouse count must match the requested schema before it becomes ready. The build/check and ramp-up phases occur outside the timed transaction-counter window.
- `--virtual-users` counts workload VUs. HammerDB's timed driver adds monitor VU1, so total VUs are `virtual-users + 1`.
- The adapter sets `raiseerror=true` and a large `total_iterations` value. It rejects a nonzero CLI status, an error message, failed or premature workload VU completion, missing monitor markers, or a malformed native result.
- The primary metric is HammerDB NOPM. TPM is indexed with separate backend-specific source and type names because TPM values are database-engine-specific. Neither value is represented as audited TPC-C throughput.
- CDM period timestamps use the adapter's wall-clock receipt of HammerDB's monitor counter-window lines. A monotonic clock verifies the requested duration. The manifest records that line receipt may lag the underlying counter snapshot.
- The startup/schema/runtime bound includes each adapter stage timeout and is only an upper-bound estimate for Rickshaw. Database, schema, VU and storage checks remain sample validity gates.

## Storage and resources

- `--data-dir=.` and `--log-dir=.` select a fresh adapter-owned root under `/tmp`, not Rickshaw's mode-0700 sample directory. The backing filesystem is recorded; this default is for mechanics checks and does not establish storage parity. Sanitized diagnostics remain in the sample directory.
- Real runs set each path to an absolute existing mountpoint visible inside the engine. The adapter verifies the path against `/proc/self/mountinfo`, records mount target, source, filesystem, device and available space, and uses only a uniquely named child.
- Resource preflight records host `MemAvailable`, every visible cgroup ancestor limit/current usage, allowed engine CPUs, and filesystem free bytes. PostgreSQL sets `max_wal_size=1GB`, a soft checkpoint target, and preflight reserves 2 GiB for the WAL mount. Memory and free-space thresholds are snapshots with concurrent-use and hidden-ancestor limitations; they are not capacity guarantees.
- The first implementation rejects `HK_CPUS` or `WORKLOAD_CPUS`; run it with CPU partitioning disabled. It makes no CPU or NUMA placement claim.

## Native option references

- [MariaDB InnoDB system variables](https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables) documents `innodb_log_group_home_dir`; [mariadb-install-db](https://mariadb.com/docs/server/clients-and-utilities/deployment-tools/mariadb-install-db) documents private option-file handling.
- [PostgreSQL initdb](https://www.postgresql.org/docs/13/app-initdb.html) documents `--waldir`.
- HammerDB's [schema build](https://www.hammerdb.com/docs/ch09s05.html) and [schema check](https://www.hammerdb.com/docs/ch04s05.html) documentation describe native completion records and checks required before measurement.
