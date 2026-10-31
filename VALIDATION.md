# Validation record

This file is updated with observed evidence. A planned procedure is not counted as a completed check.

## Static checks

- `python3 -m unittest discover -s tests -v`: 34 focused adapter, parameter, storage, native-output, measurement, EL8 MariaDB compatibility, backend-aware schema-gate, process-group cleanup, stdout-supervision and release-aware post-processing tests passed.
- Port-probe regression tests created an actual loopback `TIME_WAIT` entry and confirmed it is reusable; live listeners bound to `127.0.0.1` or `0.0.0.0` are rejected with `SO_REUSEADDR` both enabled and disabled.
- `python3 -m py_compile` for the shared module, client, postprocessor and tests passed.
- `bash -n hammerdb-base hammerdb-client` passed.
- JSON parsing passed for Rickshaw, Multiplex, Workshop, benchmark metadata and the example run file; all five passed Draft 7 validation against the local Crucible schemas.
- `git diff --check` passed.
- The 18 HammerDB release command entries passed `bash -n`; the two EL8 Python-alternative commands have since been removed in the inherited-Python experiment. The decoded checksum formatter was exercised with sample values and emitted a final newline byte.

## Native HammerDB packaging checks

- The downloaded official v6.0 RHEL 9 x86_64 archive matched SHA-256 `b4e0b1c84f9ac74802c86628b3ebca527ef314404be68827fca30174147a92b6`.
- The official v5.0 RHEL 8 x86_64 archive matched its published SHA-1 and the locally pinned SHA-256 before its Alma 8 and RHUI 8 runs.
- The packaged HammerDB v6.0 CLI identified itself correctly. `dbset`/`diset`/`loadscript` generated MariaDB and PostgreSQL workload scripts with private TMP/HOME directories and no database connection. This proves script-generation compatibility only.
- Explicit `package require mariatcl` and `package require Pgtcl` checks failed on this development host because its dynamic linker lacks `libmariadb.so.3` and `libpq.so.5`. The corrected mounted trial subsequently passed actual selected-package loading for all eight samples in the prepared Alma 9 userenv, along with live database connectivity. This confirms the pinned clients and provisioned libraries work in that target image.
- The detailed mounted trial exercised the prepared Alma 9 userenv; the separate ten-profile smoke evidence is recorded below. Neither establishes a normal published installation or behavior on other architectures.

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

## Broader userenv compatibility smoke runs

The same remotehosts endpoint configuration used for the bench-stream, bench-coremark and bench-phpbench expansion was used for one MariaDB sample per profile. Each run used x86_64, one warehouse, two workload VUs, one minute of ramp-up, one minute of measurement, and the mechanics-only `data-dir=.` / `log-dir=.` defaults, with CPU partitioning disabled. All ten Crucible runs exited successfully, produced a valid 60-second NOPM period, and indexed their results.

HammerDB 6.0 passed on the eight default-profile userenvs. Its official RHEL 9 binary could not start on EL8 because the required GLIBC versions are not available there. Alma 8 and RHUI 8 therefore select the official HammerDB 5.0 RHEL 8 build; both passed the same end-to-end smoke path, including post-processing and indexing. The version split is per profile; HammerDB was not downleveled across the matrix.

| Userenv | HammerDB | Run ID | NOPM |
| --- | --- | --- | ---: |
| alma8 | 5.0 | `ce0d5027-d17b-4b53-a9e3-0db907a86b43` | 9,040 |
| alma9 | 6.0 | `8f696f3f-3c3e-4703-81ae-9b88970f8fc7` | 17,262 |
| alma10 | 6.0 | `48b46770-209a-47a0-a738-6f0869979781` | 18,725 |
| fedora43 | 6.0 | `b48f8541-c962-4f56-b9c0-bcc0f3a49349` | 18,420 |
| fedora44 | 6.0 | `609dba04-b71a-4d98-b056-d9537ce517c9` | 17,522 |
| rhubi8 | 5.0 | `9c0d29cd-0031-4a2c-8561-e42a18dec8b9` | 8,363 |
| rhubi9 | 6.0 | `933b4fc5-6f67-4e01-b68c-d82fe7f16e5c` | 18,319 |
| rhubi10 | 6.0 | `42f10f1f-e8ec-4b21-a0ae-ad093849978e` | 17,977 |
| stream9 | 6.0 | `db8fa3db-808f-4577-8b1a-eaa01afb9930` | 17,447 |
| stream10 | 6.0 | `ce2728cf-ac8b-416c-9010-a1e4938ef365` | 18,941 |

These one-sample NOPM values confirm that each profile completed its workload and emitted an indexed result. They are not cross-userenv performance comparisons. The wider smoke runs used dot-default temporary storage and do not establish storage parity.

### Workshop release-command recheck

After splitting each release requirement into sequential, individually logged commands, the final Workshop stage rebuilt successfully for one EL8 and one EL9 profile. A marker file carries the temporary download path between shell invocations; failed commands leave that directory for inspection, while the final success step removes it. Both one-sample MariaDB runs used the same remotehosts endpoint and completed post-processing and OpenSearch indexing.

| Userenv | HammerDB | Run ID | Measurement period | NOPM |
| --- | --- | --- | ---: | ---: |
| alma8 | 5.0 | `25652f95-8df0-4370-9d8c-e16ab8c207f1` | 60.057 s | 8,785 |
| alma9 | 6.0 | `b354dfc4-4cc4-4186-aecc-cf091b6da0b3` | 60.053 s | 17,326 |

These runs verify the successful Workshop install path and benchmark lifecycle for both releases. They did not force a requirement failure to inspect the preserved temporary directory.

## EL8 inherited-Python package experiment

On 2026-10-30, removed the distro `python3` package from `hammerdb_runtime_packages` and removed the EL8-only alternatives requirement. The Alma 8 and RHUI 8 Workshop stages both built, and each one-sample x86_64 MariaDB run completed and indexed using HammerDB 5.0, one warehouse, two workload VUs, one minute of ramp-up, one minute of measurement, CPU partitioning disabled, and mechanics-only `data-dir=.` / `log-dir=.` defaults.

| Userenv | Run ID | Measurement period | NOPM |
| --- | --- | ---: | ---: |
| alma8 | `795d28c2-9236-42ef-8520-c23c158e2022` | 60.048 s | 8,803 |
| rhubi8 | `508bf1a6-60fe-4cdb-8eea-2ac481d020d4` | 60.040 s | 9,587 |

This confirms the tested EL8 profiles can inherit Python 3.12 from the Crucible engine base image without the `python3` distro package or an alternatives step. The EL8 profile remains necessary to select HammerDB 5.0 because the HammerDB 6.0 RHEL 9 binary needs a newer GLIBC. These one-sample runs are smoke compatibility evidence, not performance acceptance or a cross-userenv comparison.

## Adapter SIGTERM trials

Dedicated MariaDB and PostgreSQL trials each sent SIGTERM after HammerDB emitted its timed counter-window start marker and workload-worker startup records. Neither run produced a score. Both adapters recorded `RunInterrupted`, marked the sample invalid and retained no measurement timestamps. In each run the harness's client-start-end roadblock reported `rc=4`, so the iteration was unsuccessful; no metric descriptors, metric data or measurement-period documents were indexed. This is the roadblock result, while HammerDB had not produced a final workload result.

Independent review confirmed all three observed owned process groups in each run stopped, both owned data and transaction-log children were removed, and the caller-owned mount roots remained empty with their original ownership, mode and filesystem identity. No generated benchmark password or private HammerDB/database configuration remained in retained benchmark artifacts. PostgreSQL's process age at signal receipt was 11.28 seconds; this is not a measured workload duration or measurement boundary. These trials establish adapter SIGTERM cleanup for both backends, not controller cancellation or SIGINT handling.

## Limits

The multi-sample, two-backend evidence remains specific to Alma 9 x86_64. The ten-profile smoke matrix establishes one-sample MariaDB compatibility on x86_64 only; ARM, broader CI, concurrent clients, controller cancellation, SIGINT, and a normal published installation remain untested. Monitor timestamps are adapter receipt times for HammerDB counter-window messages; their possible arrival delay remains, and no strict independently observed setup/ramp-up boundary is claimed. The trials do not establish production performance or independent storage-device parity. The source hashes reviewed for the earlier matrix identify runtime tree `d529cde07a87deb4637a45bfb25f15d039fb8c69`; the broader userenv support changes runtime and Workshop files as well as documentation.
