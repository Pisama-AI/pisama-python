# Hosted diagnosis execution reporting

**Status:** Active
**As of:** 2026-09-10
**Owner:** engineering
**Evidence scope:** documentation-only candidate

The canonical hosted example currently prints findings without displaying
detector execution coverage. A separately tested backend candidate exposes a
sanitized execution summary, but it is not yet deployed. Display that summary
when present and explicitly report unavailable when absent. Explain incomplete
and partial results without claiming detector accuracy or a completed workflow.

Do not change SDK runtime, dependencies, version, authentication, ingestion,
package publishing or deployment. Preserve the existing hosted example and
its warning that ingestion is asynchronous. This update is safe before the
backend change: absent metadata means unavailable, not an assumed clean run.

Verify Markdown Python blocks compile and execute only the extracted reporting
statements against local complete/partial/failed/missing inputs; no network,
credential reads or customer files. Review the exact diff before publication.
DONE: reviewed docs and local example checks; no SDK/runtime score credit.

## Local evidence

All five Python documentation fragments compile with top-level await permitted
for the pre-existing async examples; the first ordinary-script compile attempt
failed on their top-level await and is not recorded as a pass. The hosted
example itself parses as ordinary Python. Executed only its three new reporting
statements against eight local inputs: missing, null, non-object and empty
metadata, then complete/partial/failed/unavailable. All eight passed. No network,
credential prompt, file ingestion or SDK method was executed. The example
reports server-supplied status; it is not an independent coverage verifier.
