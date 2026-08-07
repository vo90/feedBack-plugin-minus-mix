# MinusMix

Creates a new, single-stem `.feedpak` for instrument practice from a normal
single-stem or multi-stem local song. The source package is always read-only.

For selected stems `S`, the rendered backing is:

```text
MinusMix output = original full mix - sum(S)
```

For a normal single-stem source, the plugin talks directly to the public HTTP
API of the model server managed by Stem Splitter. It requests the selected
stem(s) into a caller-owned temporary directory. The model may calculate all
six sources internally, but none are written into the source feedpak and the
entire temporary directory is deleted after export. If the source already
carries the selected stems, they are reused as a fast path. The exporter also
asks compatible servers to delete that job's result cache after the requested
download; the server's normal TTL and maximum-job cleanup remain a fallback if
that best-effort request fails. Requested stems are streamed to the temporary
workspace rather than buffered in memory, and unrequested outputs are not
downloaded when the server uses recognisable standard stem labels.

The subtraction happens on decoded audio in FFmpeg and the result is encoded
once to Ogg Vorbis. Every arrangement, lyric track, rig, cover and other
non-stem asset is copied into a new zip-form feedpak. The manifest is rewritten
to one `full` stem and the preview is rebuilt from the MinusMix audio.

## Safety contract

- Never edits, renames or deletes the source package.
- Never overwrites an output; collisions gain ` (2)`, ` (3)`, etc.
- Builds a temporary archive in the destination folder and atomically renames it.
- Rejects unsafe/duplicate archive member paths and escaping directory symlinks.
- Accepts output-folder writes only from a loopback client, so optional LAN mode
  does not expose an arbitrary filesystem-write endpoint.
- Uses a folder chosen through the desktop shell's native directory dialog.

## Optional-plugin and server relationship

MinusMix is a standalone, optional, user-installed plugin. It is
not a FeedBack core component and does not require a FeedBack or Stem Splitter
code update. It discovers the local server state/config files used by current
Stem Splitter releases and calls the server's existing `/health`, `/separate`,
`/jobs` and download endpoints. The usual workflow is simply to start the local
server from Stem Splitter and then open MinusMix. Existing standard
`manifest.stems` entries remain a server-free fast path.

The self-contained client also understands the app's existing
`demucs_server_url` and API-key settings. Credentials are sent only to the
configured server origin and are stripped from cross-origin download redirects.
The plugin never imports Stem Splitter's Python modules or reaches into its
runtime objects.

Audio stems are distinct from notation arrangements. The standard six-stem
models emit one combined `guitar` stem, so excluding it removes estimated lead
and rhythm guitar audio while leaving all lead/rhythm charts in the new pack.

## Batch mode

Batch mode scans `.feedpak` and `.sloppak` files recursively and recreates the
source folder structure below a separately chosen output folder. It skips
existing outputs and songs previously derived by MinusMix by default, records
per-file failures without stopping the queue, persists recent job status, and
supports safe cancellation.

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
- Compatible with the current main/nightly Stem Splitter server HTTP contract.
- Direct conversion of an unsplit FeedPak requires that server to be running.
- Converting a FeedPak that already contains the selected stems does not require
  the server.

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
download its server/models once, and start that server before exporting from an
unsplit song. MinusMix does not replace, patch or update Stem
Splitter and does not download model weights itself.
