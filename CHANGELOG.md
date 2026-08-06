# Changelog

## 0.4.0

- Made Practice Mix Exporter a completely standalone optional plugin.
- Replaced the unpublished in-process Stem Splitter hook with a self-contained
  client for the released model server HTTP API.
- Added current local-server discovery, remote-server/API-key support, guarded
  redirects, streamed downloads, progress polling, cancellation and cleanup.
- Added single-song and recursive batch exporting with non-destructive output,
  duplicate-audio reuse and persistent batch status.
- Added a standalone test suite with real FFmpeg export coverage.

