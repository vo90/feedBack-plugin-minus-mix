# MinusMix

Creates a new, single-stem `.feedpak` for instrument practice from a normal
single-stem or multi-stem local song. The source package is always read-only.

For selected stems `S`, the rendered backing is:

```text
MinusMix output = original full mix - sum(S)
```

For a normal single-stem source, the plugin talks directly to the public HTTP
API of the managed local model server started from Stem Splitter. It requests
the selected stem(s) into a caller-owned temporary directory. The model may
calculate all six sources internally, but none are written into the source
feedpak and the entire temporary directory is deleted after export. If the
source already carries the selected stems, they are reused as a fast path. The
exporter also asks the managed server to delete that job's result cache after
the requested download; the server's normal TTL and maximum-job cleanup remain
a fallback if that best-effort request fails. Requested stems are streamed to
the temporary workspace rather than buffered in memory, and unrequested outputs
are not downloaded when the server uses recognisable standard stem labels.

The subtraction happens on decoded audio in FFmpeg. The playable mix and its
optional preview are normally rendered together from one decoded filter graph;
an independent preview fallback preserves compatibility without failing the
main export. Every arrangement, lyric track, rig, cover and other
non-stem asset is copied into a new zip-form feedpak. The manifest is rewritten
to one `full` stem and the preview is rebuilt from the MinusMix audio.

## Safety contract

- Never edits, renames or deletes the source package.
- Never overwrites an output; collisions gain ` (2)`, ` (3)`, etc.
- Builds a temporary archive in the destination folder and publishes it with an
  atomic no-replace strategy.
- Rejects unsafe/duplicate archive member paths and escaping directory symlinks.
- Accepts output-folder writes only from a loopback client, so optional LAN mode
  does not expose an arbitrary filesystem-write endpoint.
- Sends full-mix separation requests only to Stem Splitter's managed loopback
  server; configured remote/custom servers are never contacted by MinusMix.
- Uses a folder chosen through the desktop shell's native directory dialog.

## Optional-plugin and server relationship

MinusMix is a standalone, optional, user-installed plugin. It is
not a FeedBack core component and does not require a FeedBack or Stem Splitter
code update. It discovers the managed local server state/config files used by
current Stem Splitter releases and calls that loopback server's existing
`/health`, `/separate`, `/jobs` and download endpoints. The usual workflow is
simply to start the managed local server from Stem Splitter and then open
MinusMix. Existing standard `manifest.stems` entries remain a server-free fast
path.

This release deliberately supports one separation path: **Stem Splitter's
managed local HTTP server**. Stem Splitter's in-app engines, managed Docker
sidecar, and remote/custom `demucs_server_url` servers are not supported yet.
MinusMix ignores remote URL and API-key settings and never imports Stem
Splitter's Python modules or reaches into its runtime objects.

Audio stems are distinct from notation arrangements. The standard six-stem
models emit one combined `guitar` stem, so excluding it removes estimated lead
and rhythm guitar audio while leaving all lead/rhythm charts in the new pack.

## Batch mode

Batch mode scans `.feedpak` and `.sloppak` files recursively. By default it
recreates the source folder structure below the chosen output folder. Users can
instead select flat output, which writes every generated FeedPak directly in
the output folder and gives filename collisions deterministic numbered names.
The batch skips existing outputs and songs previously derived by MinusMix by
default, records per-file failures without stopping the queue, persists recent
job status, and supports safe cancellation.

Folder scanning runs as an observable background job and can be canceled
between packages. Starting a batch reuses the completed authoritative scan when
source file signatures and planned output existence are unchanged; otherwise it
automatically scans again before writing anything.

Separation runs sequentially to avoid GPU-memory contention. Feedpaks with a
saved selected stem bypass the model. For unsplit sources, a byte-identical full
mix encountered again in the same batch reuses the requested temporary stem;
the reuse cache is deleted when the job finishes. No bitrate, sample-rate, model
quality, or analysis-window reduction is used as a speed optimization.

Single-song exports also run as background jobs. The screen reports validation,
separation, rendering, preview and packaging stages, and safe cancellation can
stop both the separator and FFmpeg rendering. Selecting an already-derived
MinusMix output produces a quality warning so users can return to the original
FeedPak instead of applying another lossy generation accidentally.

## Compatibility

- Installable through FeedBack's Plugin Manager from a standalone Git repository.
- Compatible with the current main/nightly managed local Stem Splitter server
  HTTP contract.
- Direct conversion of an unsplit FeedPak requires that managed local server to
  be running and ready.
- Converting a FeedPak that already contains the selected stems does not require
  the server.
- Remote/custom servers, Docker sidecars and Stem Splitter's in-app engines are
  outside the current support scope.

## Development

The full test suite requires Python 3.10 or newer, FFmpeg on `PATH`, and Node.js.
Install the development dependencies, then run the same lint, test, and package
checks used by CI:

```text
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
python scripts/build_release.py
```

The real-audio tests are skipped when FFmpeg is unavailable. Because those tests
contribute to the coverage floor, a run without FFmpeg is only a partial check
and may correctly finish below the required coverage percentage.

## Installation without Git

Download the versioned `MinusMix-<version>.zip` from the
[latest MinusMix release](https://github.com/vo90/feedBack-plugin-minus-mix/releases/latest),
close FeedBack, and extract the archive directly into FeedBack's `plugins`
directory:

| Platform | Plugin directory |
| --- | --- |
| Windows | `%APPDATA%\feedback-desktop\plugins` |
| macOS | `~/Library/Application Support/feedback-desktop/plugins` |
| Linux | `~/.config/feedback-desktop/plugins` (or `$XDG_CONFIG_HOME/feedback-desktop/plugins`) |

The finished layout must be `plugins/minus_mix/plugin.json`. If it instead
looks like `plugins/minus_mix/minus_mix/plugin.json`, move the inner folder up
one level. Restart FeedBack and MinusMix will appear in the Plugins section.

Manual installations do not need Git. FeedBack's Git update button will not
apply to them: to update, close FeedBack and replace the existing `minus_mix`
folder with the one from the newest release ZIP. MinusMix stores no user
FeedPaks or exports inside its own plugin folder.

## Installation with Git

This repository's root is the plugin root: `plugin.json` is intentionally at the
top level, exactly as FeedBack's Plugin Manager expects. Paste this URL into
**Settings → Plugins → Install from Git URL**, install it, and restart FeedBack:

```text
https://github.com/vo90/feedBack-plugin-minus-mix.git
```

Updates then use the Plugin Manager's normal Git update button.

Stem Splitter remains a separate optional plugin. Install its current release,
download its managed local server/models once, and start that server before
exporting from an unsplit song. MinusMix does not replace, patch or update Stem
Splitter and does not download model weights itself.

## License

MinusMix is available under the [MIT License](LICENSE).
