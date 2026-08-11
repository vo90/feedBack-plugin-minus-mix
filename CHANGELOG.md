# Changelog

## 0.6.0

- Bounded batch status snapshots before copying rows, keeping polling work and
  memory stable for very large queues.
- Made final FeedPak publication atomic across app processes so a file that
  appears during export is preserved and receives a numbered neighbour.
- Continued server discovery past responding but unavailable local targets so
  a ready configured server can be used.
- Filtered the song-index query to FeedPaks in the database and prevented stale
  asynchronous search responses from replacing the current result set.
- Moved MinusMix from the Audio pedalboard to Tools.
- Added a batch output choice between preserving the selected source folder
  structure (the default) and writing every generated FeedPak directly into a
  flat output folder, with deterministic numbered names for collisions.
- Reused unchanged authoritative folder scans when starting a batch, added
  observable/cancelable background scanning, and made queue progress updates
  constant-time.
- Bounded persisted history to 400 actionable rows per recent job and moved
  JSON encoding and disk I/O outside the batch-state lock.
- Released full terminal queues from process memory after preserving a bounded
  actionable history, and pruned the in-memory job store to the five newest
  summaries while always retaining active work.
- Parsed each source manifest once, reused one validated package session for
  initial extraction, and calculated duplicate-audio hashes while extracting.
- Reused short-lived server discovery results and split the server client into
  upload, polling, and streamed-download stages.
- Rendered the final mix and optional preview from one FFmpeg decode graph,
  retaining the independent renderer as a compatibility fallback.
- Made Stem Splitter status contextual, surfaced optional preview failures,
  added keyboard-accessible tabs and form/progress semantics, and moved the
  batch skip toggles under a closed Advanced section.
- Added committed Ruff rules and a 70% core-module coverage floor; the expanded
  suite now covers background scan cancellation and prepared-source reuse.
- Split exporting into explicit prepare, extract, stem-provider, render,
  package-plan and atomic-publication stages, and moved batch duplicate caching
  behind the same structural StemProvider interface used by single exports.
- Reduced the batch worker to queue coordination plus isolated per-item
  processing, introduced typed job/count/run-context boundaries, and persisted
  completed jobs as summaries without historical rows.
- Added executable frontend state tests, a real spawned-process no-overwrite
  race test, persistence-failure coverage and preview audio regression checks;
  tightened the committed complexity ceiling from 35 to 20.
- Replaced the monolithic route-setup closure with a dependency-owned API
  composer while preserving every existing endpoint and response contract.
- Centralized browser requests in a small testable API client so UI controllers
  no longer construct transport paths, methods, headers, or JSON bodies.

## 0.5.1

- Made long-running batch progress memory-stable by updating existing result
  rows instead of rebuilding the full list on every status refresh.
- Throttled job polling when MinusMix is not visible and restored authoritative
  status when the user returns, while retaining completion notifications.
- Added a live status timestamp, manual progress refresh and bounded large-batch
  result views that retain active and failed items.
- Limited accessibility announcements to concise progress text rather than the
  complete changing result list.

## 0.5.0

- Renamed the plugin from Practice Mix Exporter to MinusMix.
- Changed the public plugin id and API namespace to `minus_mix` before the
  first public release.

## 0.4.0

- Made Practice Mix Exporter a completely standalone optional plugin.
- Replaced the unpublished in-process Stem Splitter hook with a self-contained
  client for the released model server HTTP API.
- Added current local-server discovery, remote-server/API-key support, guarded
  redirects, streamed downloads, progress polling, cancellation and cleanup.
- Added single-song and recursive batch exporting with non-destructive output,
  duplicate-audio reuse and persistent batch status.
- Added a standalone test suite with real FFmpeg export coverage.
