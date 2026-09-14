# MinusMix: local stem server compatibility and update recovery

Implementation plan, 2026-09-14. Product implementation has not started.

## Objective and required compatibility

MinusMix must continue creating exports with the local stem server after the user installs it, updates it, or restarts it. Supporting the older server currently used with Nightly is a release requirement. Installing this MinusMix change must not require an accompanying Stem Splitter/server upgrade.

Ordinary server, model and dependency updates that retain the supported public API should not require another MinusMix release. During a recoverable update interruption, keep the current export waiting and continue automatically. Do not mark every remaining batch item failed because the shared server is temporarily unavailable.

Use the public HTTP contract and advertised capabilities, not server/plugin version numbers, private Python imports, installation paths, dependency versions, GPU names or particular model checkpoint hashes. Preserve the existing local-server-only support scope. The current public API cannot guarantee compatibility with an arbitrary future breaking API change or exactly-once server computation after a lost upload response; handle those limits explicitly without corrupting or duplicating published exports.

## Repository and branch

- Repository: `vo90/feedBack-plugin-minus-mix`.
- Implementation branch: `feat/minusmix-managed-server-compat`.
- Base: `9389438702b2e5a24d076ef75895e19d8e6c292f`, the newest local MinusMix development code, including all audio-reuse work. GitHub main `274f345b8a990ca6e1d236a3217f246b4aa9f0e5` / 0.6.2 is an ancestor, seven commits behind.
- The separation client and existing single/batch/export paths are identical in those two revisions, so the integration defects affect both.
- Keep the Stem Splitter plugin at `1a3875abbba1335fa1bc63827070b15e3014521c` and the updated server at `d9edcc85156d77eb4bedc17e4dcd90ed37eb5335` unchanged.
- No game-core, Desktop, server or Stem Splitter source changes. No pushes, releases or merges as part of this plan. No edits to normal user settings, installed servers or song libraries.

## Behavior to deliver

| Situation | Required MinusMix behavior |
|---|---|
| User keeps the older Nightly server | Continue using its existing local HTTP API; no upgrade requirement. |
| User installs/starts the server after opening MinusMix | Refresh availability automatically and enable separation without restarting the game. |
| Updated server has a verified BS model and startup state `skipped` | Accept a separation request immediately; do not ask the user to download the installed model again. |
| Older server skips startup loading and has no verified inventory | Describe the server as available for an on-demand attempt; do not claim its model was verified. Model loading occurs only after an explicit export request. |
| Managed server does not contain the selected model or required stems | Give an accurate setup message; do not pretend it is ready or silently select another model/server. |
| Server starts an update while an export is running | Show waiting/reconnecting, preserve the current item and queued items, then recover automatically when possible. |
| Completed old job is retained after an update | Download that same result, even if the replacement generation no longer contains the old model. |
| Old job/result was lost or expired | Permit one bounded recovery recomputation of the original input/model/stem request when compatible; explain the restart and discard the old partial stem set. |
| Source already contains the required stems, or user uses donor-audio reuse | Complete without contacting or waiting for a server. |

## 1. Add capability-based server assessment

Concentrate discovery and protocol logic in `separator_client.py`. Preserve the existing `ready`, `reason`, `engine`, `source`, `model`, `device`, `gpu` and `supported_stems` response fields. Add structured state rather than parsing human messages: for example `ready`, `on_demand`, `warming`, `updating`, `reconnecting`, `unavailable`, `missing_model`, `unsupported_stems` and `incompatible`. Add a `waitable` flag so UI and batch admission distinguish temporary service interruption from a configuration problem.

Implement two explicitly tested compatibility paths:

**Recognized managed contract:** use type-checked `health.runtime`, schema 1 and `verified_models_v1`. For a new request, require the selected model's `verified` value to be exactly true and require every requested instrument in its advertised stem set. Verified assets plus intentionally skipped startup loading are usable. Explicit failures still need an accurate error. Honor `runtime.activity.draining` / `sealed` as temporary admission states. Never downgrade an explicit missing-model or unsupported-stem denial into an unrestricted legacy attempt.

**Older/basic HTTP contract:** a missing runtime object or unsupported optional `/runtime` endpoint is normal. Retain the existing `/health`, upload, job and download workflow. Understand ready states and allow an explicit on-demand request for legacy `skipped` or absent model-state information without calling that model verified. Treat pending/loading/download states as temporary and explicit model failures as failures. Resolve the `demucs` warmup alias only when the reported `demucs_model` identifies the selected model; do not misapply one model's warmup state to another.

Ignore additive fields and unrecognized optional capabilities. For an unknown future metadata schema, do not assume its verified-model/persistence semantics. A clearly identified basic HTTP compatibility path can continue where ordinary health and job contracts still match; malformed recognized metadata or explicit API denials must not be bypassed. A genuinely incompatible core API produces an actionable error rather than a crash or a false success.

Status checks must not install dependencies, download weights, start/stop the server, or invoke private updater operations. Stem Splitter remains responsible for installation and management.

## 2. Bind requests to the intended local server

Continue discovering settings through `stem_splitter.json` and `stem_splitter_server.json`. Make port precedence explicit: the recorded running endpoint represents the current process; the configured endpoint represents the user's intended setting; port 7865 is a historical default only when no explicit usable configuration exists.

When a configured server is present but warming, updating or missing a model, do not fall through to another ready service on 7865. When it is temporarily absent, wait for that endpoint instead of silently using another installation. Report conflicting stale state/configuration clearly.

At export start, snapshot the selected logical model ID and requested stem set. Once work is submitted, retain its origin, job ID, source/input identity and any understood runtime/result metadata. Re-read installation state after errors and invalidate short-lived readiness caches, but do not automatically move an in-flight job to a different port or silently change the selected model. An explicit endpoint change requires a new operation or an actionable blocked state; a generation change at the same intended endpoint is an expected update.

Readiness for new submissions and retrieval of an already accepted job are different operations. New runtime inventory must not prevent retrieval of an old completed job.

## 3. Implement bounded recovery across each network phase

Use an explicit operation context and a recovery loop:

`resolve -> submit -> poll known job -> download partial files -> complete local stem set`

A temporary connection failure, explicit update/drain response or eligible HTTP 503 enters `waiting_for_server`, then resumes the same phase. Honor a bounded `Retry-After` where provided. Keep requests and backoff waits cancel-aware. Do not repeatedly reset deadlines when another failure occurs.

- **Before upload:** wait for an explicitly waitable server. If no server has ever been configured/installed, show setup guidance rather than silently waiting indefinitely.
- **Explicit rejected upload:** retry busy/draining responses within the shared recovery budget.
- **Ambiguous upload outcome:** a lost POST response does not prove the server rejected it. Do not claim exactly-once inference. After the same intended server is healthy, permit at most one recovery resubmission using the same input/model/stems, with a visible message and a shared attempt limit. Existing server deduplication may avoid extra work, but cannot guarantee this across a cutover. Guarantee one final local publication, not one GPU computation.
- **Known job ID:** always try that ID first after a restart, including across managed generation changes. Treat job IDs as opaque; do not reproduce the server's private fingerprint/ID algorithm in MinusMix or search `/jobs` as a complete recovery index.
- **Polling:** retry eligible transient errors. A confirmed healthy-server 404 is missing/expired work, not evidence of an endless update. Explicit terminal inference failures and authentication/validation errors retain their useful error messages.
- **Downloads:** retain the original result descriptor. Retry interrupted files into `.part` files from byte zero unless a separately verified range protocol exists. Validate nonempty, readable audio and the complete requested stem set before accepting it. Never append blindly or combine files from different recomputation attempts.
- **Lost results:** allow one complete recovery recomputation when the configured logical model and required output contract remain available. Keep the original input alive and discard only caller-owned partial results from the abandoned attempt. If the selected model receives new weights, explicitly show that this song's separation is restarting with the updated model; do not silently combine old/new stems. An incompatible engine/stem contract or removed selected model is a setup block, not permission to substitute another model.
- **Cleanup:** do not call server cache deletion on a recoverable polling/download failure. Content-addressed cache entries are shared and the API exposes no consumer lease. Keep retrieval possible until local download is complete; do not treat an ID as an ownership token or delete an old job through an unrelated/replacement service. Revisit existing terminal cleanup conservatively and rely on the server's bounded cache retention where ownership is uncertain.

Use a cumulative recovery budget of up to **35 minutes**, separate from the existing 35-minute useful-processing budget, with a total per-separation budget capped at 70 minutes. This is necessary because the current updater can stop the old server and spend up to 30 minutes validating the candidate before starting its replacement. These are upper bounds, not fixed waits; resume as soon as possible. Share retry/recompute budgets across nested operations, including existing incomplete-result retries, so repeated errors cannot create an unbounded loop.

Recovery sleeps should check Cancel at least every 250 ms and health probes should be short. Cancellation during existing blocking upload/read calls is subject to bounded I/O timeouts; do not promise instantaneous interruption without redesigning those calls. Prevent any later retry/publication after cancellation is observed.

## 4. Keep single exports and batch queues usable

Use an optional structured state callback alongside progress text. During automatic recovery, keep the job active with `stage="waiting_for_server"`, preserve progress and output locks, and expose Cancel. Do not add a separate manual Pause/Resume feature in this change.

Update the single and batch UI readiness gates and the batch backend admission gate to allow ready/on-demand service and explicit waitable update states. Maintain immediate setup feedback for hard missing-model/capability errors. Jobs that reuse saved stems remain independent of these gates.

Hold the current batch item inside the client's recovery loop while later items remain queued. If recovery expires or a shared-service configuration block occurs, stop the batch with an explicit blocked/interrupted outcome and retain pending rows and accurate counters. Do not route it through the generic per-song failure-and-continue handler, and do not let `_finish_run()` label an interrupted batch completed at 100%. Genuine source-specific errors may continue using the existing per-song failure behavior.

On a later user retry, the existing scan/output checks should recognize completed outputs and process only unfinished work. Completed files remain untouched. Existing app-restart recovery semantics are not expanded into unattended cross-session job recovery by this change.

Use plain messages, such as:

- "Server available — model loads when needed."
- "Waiting for the stem server to finish updating. Your export will continue automatically."
- "Reconnecting to the stem server."
- "Server updated — restarting separation for this song."
- "The selected model is not installed. Open Stem Splitter to install it."

## 5. Expected file scope

| File(s) | Change |
|---|---|
| `separator_client.py` | Capability assessment, discovery binding, typed transient/configuration errors, operation context, retry/recovery and safe partial downloads. |
| `single.py` | Map client state to waiting/progress; retain active job/cancellation semantics. |
| `batch.py` | Waitable admission, current-item waiting, shared-service blocked outcome and preserved queue/counters. |
| `screen.js` | Accurate readiness/on-demand/waiting states and controls for single/batch exports. |
| Relevant client, single, batch, route and frontend tests | Old/new server contract matrix, recovery scenarios and UI/queue regressions. |
| `README.md`, `CHANGELOG.md` | Explain supported local servers, automatic recovery and practical limits. |

Avoid changes to `exporter.py`, `reuse_*.py`, plugin registration, package versions or release tooling unless implementation demonstrates a concrete need. Routes currently pass status through; change `routes.py` or `screen.html` only if the existing payload/control structure cannot express the required blocked state. No new runtime dependency is expected.

## 6. Acceptance tests and delivery sequence

**First, preserve the older Nightly contract.** Capture the exact server revision actually used by Nightly when validation runs and record it with the results. Test both its real public responses and compact fixtures: no runtime metadata, optional `/runtime` missing, BS ready, skipped/on-demand, HTDemucs warmup aliases, custom/default ports, successful single/batch separation and cache cleanup. No fixture may require the new server's metadata to pass the legacy path.

**Then test recognized managed metadata:** verified BS with skipped warmup; missing HTDemucs; missing requested stem; wrong metadata types; explicit failed model; updating/draining; new unrelated fields/capabilities; legacy-to-managed upgrade and rollback to legacy. No server/plugin version string is used as a readiness gate.

**Exercise transport and lifecycle failures using a controllable local HTTP test service:** stop/restart before upload, lose an upload response, interrupt polling, interrupt a streamed stem, return draining 503/Retry-After, retain an old result across a generation/model change, and lose/expire old results. Verify the resubmission cap, no mixed partials, no inappropriate DELETE, no default-port fallback, and exactly one local package publication. Use a fake clock for the 30-minute update/35-minute recovery boundary cases rather than actually sleeping.

**Exercise managers and UI:** a three-song batch loses the server during song two; song one remains done, song two waits and song three remains queued. Recovery completes the remaining items. Timeout/hard setup blocks retain pending rows without false completion. Cancel while waiting prevents later submission/publication. Navigating away/back does not duplicate jobs. Completely forbid network access in saved-stem and donor-audio reuse tests.

Run the existing configured MinusMix test suite and coverage gate after the focused compatibility/recovery tests pass. Preserve existing incomplete-result, source-protection, output-collision and atomic-publication tests.

Finally, use isolated test profiles/libraries to run a real MinusMix export against both the older Nightly local server and the unchanged updated server. Include a server update/restart during export and a cold startup with no earlier Stem Splitter job. Use CPU and available CUDA hardware without GPU-specific code; the RTX 4080 PC remains a separate hardware validation target. Do not claim full integration success based only on health fixtures or standalone server inference.

Implement in reviewable steps: contract/discovery plus legacy tests; transport recovery; manager/UI integration; full regression and real local-server validation. Record exact tested revisions and results. All product commits stay in MinusMix, and publication remains a separate decision.
