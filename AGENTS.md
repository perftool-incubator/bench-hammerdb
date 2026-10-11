# bench-hammerdb contributor instructions

- Keep this project client-only: HammerDB and its selected local MariaDB or PostgreSQL server run inside the same Crucible client engine.
- Provision software only through Workshop and the `alma9` userenv. Start and stop only owned database processes directly; do not use host systemd, SELinux changes, tuned profiles, host storage discovery, or storage formatting.
- For explicit storage selections, create only private per-attempt children under caller-selected mountpoints; record actual mount provenance and never change or remove a caller-owned root. The mechanics-only `.` default uses a private adapter-owned `/tmp` root because Rickshaw's sample parent is not traversable by the database service accounts.
- `--data-dir` selects database files and `--log-dir` selects MariaDB redo or PostgreSQL WAL. Both owned trees are removed after the owned database process stops. Sanitized diagnostic output is retained separately in the sample directory.
- Treat database files, native output, and diagnostics as untrusted until the manifest marks the sample valid and post-processing reparses the retained HammerDB output.
- Preserve native HammerDB TPROC-C behavior. Do not describe the result as audited TPC-C or call TPM `tpmC`.
- Keep the HammerDB GPL-3.0 license with the provisioned distribution. The adapter is Apache-2.0 licensed.
- Keep hosts, credentials, and infrastructure details out of the repository. Do not publish, commit, or push without the parent agent's instruction.
- Run focused local tests and schema checks after edits. Coordinate any database, root, Workshop image, or remote benchmark run with the parent agent.
