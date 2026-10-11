# HammerDB for Crucible

This project adds a client-only HammerDB subproject for native TPROC-C runs against a private MariaDB or PostgreSQL instance started inside the same Crucible client engine. It has no server role and does not require a pre-existing database service.

The initial Workshop target is the `alma9` userenv. HammerDB's official v6.0 RHEL 9 x86_64 and aarch64 release archives are selected by architecture and SHA-256 checked before installation. The pinned tag commit is [`d33f879aec858063edd17aa2daa46db03abb2bae`](https://github.com/TPC-Council/HammerDB/tree/d33f879aec858063edd17aa2daa46db03abb2bae). The full upstream GPL-3.0 text is included at [third-party/HammerDB-GPL-3.0.txt](third-party/HammerDB-GPL-3.0.txt); the adapter is Apache-2.0 licensed.

Workshop installs MariaDB and PostgreSQL server/client packages into the engine image. At runtime the adapter starts the selected daemon directly under its packaged service account, binds it to loopback on an engine-specific port, creates a fresh private database, checks the HammerDB driver and schema, runs the timed workload, stops its owned process group, and removes its private database files. The port preflight tolerates completed connections in TCP `TIME_WAIT` while rejecting a live listener; a startup bind race remains a hard failure, and the adapter never attaches to an existing database. It does not call systemd or change SELinux, tuned profiles, host disks, or caller-owned mount roots.

## Parameters

The `defaults` preset applies these defaults when no parameter set is supplied. The client parser also fills in defaults when a parameter set is partial.

| Option | Default | Supported values |
| --- | --- | --- |
| `--backend` | `mariadb` | `mariadb`, `postgresql` |
| `--warehouses` | `1` | 1 through 4 |
| `--virtual-users` | `2` | 1 through 16 workload VUs |
| `--rampup-minutes` | `1` | 0 through 30 |
| `--duration-minutes` | `1` | 1 through 60 measured minutes |
| `--data-dir` | `.` | `.` for mechanics, or an absolute existing engine mountpoint |
| `--log-dir` | `.` | `.` for mechanics, or an absolute existing engine mountpoint for transaction logs |

`--virtual-users` excludes HammerDB's timed monitor VU1. For example, two workload VUs produce three total VUs. The monitor waits through ramp-up, snapshots transaction counters, waits for the requested duration, snapshots them again, and emits NOPM/TPM.

## Storage contract

`--data-dir=.` and `--log-dir=.` create a fresh owned mechanics scratch root under `/tmp`. Rickshaw creates each sample directory with mode 0700, so the provisioned database service accounts cannot traverse it. The adapter records the actual scratch path and visible backing mount, source, filesystem, device and free bytes. The private database files are removed after the owned daemon stops. This default supports mechanics checks; it does not establish storage parity with any database disk.

For storage measurements, mount dedicated writable directories into the client engine and pass each exact mountpoint as an absolute parameter. Paths use ASCII letters, digits, slash, dot, underscore, and hyphen so Rickshaw can pass the same selection safely to post-processing. `examples/hammerdb.remotehosts.json` shows the layout with placeholder host paths. The adapter creates one unique owned child under each selected root. It never changes the root's ownership or permissions and never removes the root.

The data child contains the database cluster, private runtime sockets, generated credentials and HammerDB TMP/config/job state. The log child contains MariaDB redo files or PostgreSQL WAL. Both owned children are removed after the database process group stops; the manifest records their locations and cleanup status. `--log-dir` therefore controls the native transaction-log location. Sanitized HammerDB stage output and database stdout/stderr are written separately to the sample working directory, which Rickshaw archives with the run.

The manifest records resource snapshots, including host `MemAvailable`, visible cgroup ancestor limits and current usage, filesystem free space and allowed engine CPUs. PostgreSQL sets `max_wal_size=1GB`, which is a soft checkpoint target; preflight reserves 2 GiB for its WAL mount, a capacity heuristic rather than a hard bound. The checks cannot guarantee capacity under concurrent allocation or observe cgroup ancestors hidden from the engine.

## Workload and reporting

Every sample starts with a fresh private database, then builds and checks the requested schema before measurement. Schema readiness requires the pinned HammerDB v6 backend-specific native build marker, a successful single schema VU, the native consistency-check marker and VU result, and an SQL count matching the requested warehouse count. The HammerDB ramp-up period is outside the native transaction-counter interval. The timed driver uses `raiseerror=true` and a high `total_iterations` value so workers remain active through the monitor window.

The primary metric is native HammerDB NOPM. TPM is also retained, with distinct source and type names for MariaDB and PostgreSQL because TPM is database-engine-specific. CDM output uses only stock fields; NOPM aggregates by sum across engines, and backend-specific TPM cannot be silently combined. Results are HammerDB's native TPROC-C output; they are not audited TPC-C scores or tpmC results.

CDM timestamps are the adapter's wall-clock receipt of HammerDB's monitor counter-window messages. A monotonic clock verifies the requested interval. The manifest records that message receipt can lag the counter snapshot. A sample is invalid if HammerDB exits unsuccessfully, emits an error, reports a failed or premature worker, omits a monitor marker, produces a malformed result, or fails cleanup.

The adapter verifies the selected HammerDB Tcl client package loads before schema preparation. This checks the packaged MariaDB/PostgreSQL client library without opening a database connection; target-image connectivity still requires an actual trial. Both HammerDB and the selected local database run in the same engine. CPU partitioning is not implemented; use `cpu-partitioning: false`. The adapter rejects inherited `HK_CPUS` or `WORKLOAD_CPUS` variables rather than reporting a partitioned run as supported.

## Local checks

Focused local checks are:

```bash
python3 -m unittest discover -s tests -v
python3 -m json.tool rickshaw.json >/dev/null
python3 -m json.tool multiplex.json >/dev/null
python3 -m json.tool workshop.json >/dev/null
python3 -m json.tool benchmark-metadata.json >/dev/null
python3 -m json.tool examples/hammerdb.remotehosts.json >/dev/null
bash -n hammerdb-base hammerdb-client
```

These local checks do not start a database or run a native workload. Target-trial evidence is summarized below; the project is not yet registered in the official catalog.

## Trial evidence

The corrected runtime source tree `d529cde07a87deb4637a45bfb25f15d039fb8c69` passed an independently reviewed Alma 9 x86_64 client-only mounted mechanics matrix. It covered MariaDB and PostgreSQL, one warehouse, one- and two-workload-VU iterations, one minute of ramp-up, one minute of measurement, and two samples per iteration. All four iterations and eight samples passed. The review verified HammerDB v6 schema and timed output, actual embedded database Tcl-client loading, worker completion, exact metric API values, stock summaries, and cleanup.

Each backend used a distinct data path and transaction-log path on the same XFS backing device. The adapter placed MariaDB redo and PostgreSQL WAL at the selected log paths, removed all owned data/log children and processes after each sample, and left the caller-owned mount roots intact. This proves path selection and lifecycle behavior on that target; it does not establish independent storage devices or storage parity.

Separate MariaDB and PostgreSQL SIGTERM trials interrupted the adapter after each native timed workload had started. Both produced invalid samples with no score, measurement timestamps, measurement period, or indexed metric documents. In each trial the harness client-start-end roadblock returned code 4. Independent review confirmed the adapter's observed owned process groups and data/log children were removed while the caller-owned mount roots remained empty and unchanged. These trials cover adapter SIGTERM handling in both backends; they do not cover controller cancellation or SIGINT.

The runtime tests exercised one prepared Alma 9 x86_64 environment. ARM, broader CI, concurrent clients, controller cancellation, SIGINT, and a normal published installation remain untested. The measurements are mechanics evidence, not audited TPC-C scores or production-performance claims. The runtime hashes reviewed in the mounted matrix are those of tree `d529cde0…`; the trial-evidence edits in this file and [VALIDATION.md](VALIDATION.md) change documentation only.

## Project files

- [hammerdb_common.py](hammerdb_common.py) defines parameter, resource, storage, native-output and measurement-window contracts.
- [hammerdb-client.py](hammerdb-client.py) owns the per-sample database and HammerDB lifecycle.
- [hammerdb-post-process.py](hammerdb-post-process.py) revalidates the retained timed and schema output against the manifest and current Rickshaw engine identity before writing NOPM and backend-specific TPM CDM samples.
- [workshop.json](workshop.json) provisions the pinned HammerDB release and MariaDB/PostgreSQL packages into `alma9`.
- [rickshaw.json](rickshaw.json), [multiplex.json](multiplex.json) and [benchmark-metadata.json](benchmark-metadata.json) define the run integration and indexed output.
- [DEPENDENCY-DESIGN.md](DEPENDENCY-DESIGN.md) records the upstream pin, license, storage policy and lifecycle decisions.
- [VALIDATION.md](VALIDATION.md) records the exact checks run and their limits.
