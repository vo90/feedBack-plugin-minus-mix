# Changelog

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
