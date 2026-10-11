# Validation record

This file is updated with observed evidence. A planned procedure is not counted as a completed check.

## Static checks

- `python3 -m unittest discover -s tests -v`: 24 focused adapter, parameter, storage, native-output, measurement, backend-aware schema-gate, process-group cleanup, stdout-supervision and post-processing tests passed.
- Port-probe regression tests created an actual loopback `TIME_WAIT` entry and confirmed it is reusable; live listeners bound to `127.0.0.1` or `0.0.0.0` are rejected with `SO_REUSEADDR` both enabled and disabled.
- `python3 -m py_compile` for the shared module, client, runtime estimator, postprocessor and tests passed.
- `bash -n hammerdb-base hammerdb-client` passed.
- JSON parsing passed for Rickshaw, Multiplex, Workshop, benchmark metadata and the example run file; all five passed Draft 7 validation against the local Crucible schemas.
- `git diff --check` passed.
- The Workshop manual-install command passed `bash -n`; its decoded `printf` checksum formatter was exercised with sample values and emitted a final newline byte.

## Native HammerDB packaging checks

- The downloaded official v6.0 RHEL 9 x86_64 archive matched SHA-256 `b4e0b1c84f9ac74802c86628b3ebca527ef314404be68827fca30174147a92b6`.
- The packaged HammerDB v6.0 CLI identified itself correctly. `dbset`/`diset`/`loadscript` generated MariaDB and PostgreSQL workload scripts with private TMP/HOME directories and no database connection. This proves script-generation compatibility only.
- Explicit `package require mariatcl` and `package require Pgtcl` checks failed on this development host because its dynamic linker lacks `libmariadb.so.3` and `libpq.so.5`. The corrected mounted trial subsequently passed actual selected-package loading for all eight samples in the prepared Alma 9 userenv, along with live database connectivity. This confirms the pinned clients and provisioned libraries work in that target image.
- The successful target run exercised the prepared Alma 9 userenv. It does not establish a normal published installation or behavior on other architectures and base images.

## Corrected mounted matrix

The reviewed runtime source tree was `d529cde07a87deb4637a45bfb25f15d039fb8c69`. The matrix ran on Alma 9 x86_64 under Podman with one warehouse, one minute of ramp-up, one minute of measurement, workload VU counts 1 and 2, and two samples for each backend/VU combination. It produced four expected iterations and eight valid samples across MariaDB and PostgreSQL. Independent review accepted the complete matrix.

All eight samples passed pinned HammerDB v6 schema and timed-output validation, embedded client loading, worker-completion checks, exact native result validation, and cleanup. MariaDB 10.5.29 and PostgreSQL 13.23 were observed live. The eight NOPM measurement intervals were about 60 seconds each. Independently recomputed NOPM summaries matched the stock summary:

| Backend | Workload VUs | Mean NOPM | Sample standard deviation | Standard deviation (%) |
| --- | ---: | ---: | ---: | ---: |
| MariaDB | 1 | 52,558.5 | 768.6 | 1.46 |
| MariaDB | 2 | 82,066 | 517.6 | 0.63 |
| PostgreSQL | 1 | 68,477 | 410.1 | 0.60 |
| PostgreSQL | 2 | 111,179.5 | 485.8 | 0.44 |

These values describe this single mechanics run; they are not comparative database claims or production-performance results. All 16 metric API values matched their measurement periods. The indexed run contained 16 metric descriptors, 16 metric data documents and 8 measurement periods, with all four iterations represented. The repeated-sample TIME_WAIT port failure seen on pre-fix tree `63d80d34ed690490331bd53366dfbf929b6bd605` did not recur on the reviewed tree.

The trial used distinct data and transaction-log paths that resolved to the same XFS backing device. Live review verified MariaDB redo and PostgreSQL WAL under their selected log paths. Every sample's owned database groups and data/log children were absent after cleanup; the caller-owned roots remained empty and preserved their original ownership, mode and filesystem identity. This demonstrates path placement and cleanup, not independent storage devices or storage parity.

The pre-fix run also showed that a controller exit code of zero and `partial=false` do not prove every requested sample succeeded: failed iterations can be omitted from ordinary summaries and indexed metrics. Acceptance therefore compared expected iteration/sample counts with retained native artifacts and indexed documents.

## Adapter SIGTERM trials

Dedicated MariaDB and PostgreSQL trials each sent SIGTERM after HammerDB emitted its timed counter-window start marker and workload-worker startup records. Neither run produced a score. Both adapters recorded `RunInterrupted`, marked the sample invalid and retained no measurement timestamps. In each run the harness's client-start-end roadblock reported `rc=4`, so the iteration was unsuccessful; no metric descriptors, metric data or measurement-period documents were indexed. This is the roadblock result, while HammerDB had not produced a final workload result.

Independent review confirmed all three observed owned process groups in each run stopped, both owned data and transaction-log children were removed, and the caller-owned mount roots remained empty with their original ownership, mode and filesystem identity. No generated benchmark password or private HammerDB/database configuration remained in retained benchmark artifacts. PostgreSQL's process age at signal receipt was 11.28 seconds; this is not a measured workload duration or measurement boundary. These trials establish adapter SIGTERM cleanup for both backends, not controller cancellation or SIGINT handling.

## Limits

The evidence is limited to one prepared Alma 9 x86_64 environment and a one-warehouse mechanics workload. ARM, broader CI, concurrent clients, controller cancellation, SIGINT, and a normal published installation remain untested. Monitor timestamps are adapter receipt times for HammerDB counter-window messages; their possible arrival delay remains, and no strict independently observed setup/ramp-up boundary is claimed. The trial does not establish production performance or independent storage-device parity. The source hashes reviewed for the matrix identify runtime tree `d529cde07a87deb4637a45bfb25f15d039fb8c69`; this evidence update changes documentation only.
