# MinusMix

**Create a new practice copy of a FeedBack song with the instrument audio you
choose removed from the backing track.**

Want to play the guitar part yourself? Select **Guitar** and MinusMix creates a
new song such as `My Song (No Guitar).feedpak`. The guitar chart is still there
for you to play, but the estimated guitar audio is removed from the backing
track. Your original song is never changed.

MinusMix can exclude guitar, bass, drums, vocals, piano, other instruments, or
several of them together. It works on one song at a time or on a whole folder.

- [Why use MinusMix instead of muting a stem?](#why-use-minusmix-instead-of-muting-a-stem)
- [What you need](#what-you-need)
- [Install MinusMix](#install-minusmix)
- [Create your first MinusMix song](#create-your-first-minusmix-song)
- [Troubleshooting](#troubleshooting)
- [Technical details](#technical-details)

## What MinusMix does

MinusMix makes a separate `.feedpak` practice song. It keeps the source song's
charts, lyrics, artwork, metadata and other non-audio content, then builds a new
backing track without the selected instrument audio.

For example:

| You select | MinusMix creates |
| --- | --- |
| Guitar | A playable copy with the guitar audio removed and the guitar charts kept |
| Vocals | An instrumental-style copy with the estimated vocal audio removed |
| Guitar + Vocals | A copy with both estimated parts removed |

## Why use MinusMix instead of muting a stem?

A fully split song gives you more control, but it also stores several
full-length audio tracks and mixes them during playback. MinusMix creates a
smaller, ready-to-play practice copy with your chosen instrument already
removed. It uses less disk space, has less audio to load, always opens with the
same mix, and may preserve more of the original sound than rebuilding the song
from the remaining separated tracks.

On my own system, I have also noticed a clear improvement in playback latency
when using a normal single-track song instead of a multi-stem song, particularly
with my ASIO setup. Audio hardware and settings vary, so your experience may be
different.

Two useful terms:

- A **FeedPak** is a song package used by FeedBack.
- A **stem** is one part of a recording, such as guitar, drums or vocals.

The separation is produced by an AI audio model, so it will not be perfect on
every recording. Faint bleed or small changes to other sounds can remain. This
is normal for source separation and does not affect the original song.

## What you need

- The **FeedBack desktop app**.
- The **Stem Splitter** plugin available in your FeedBack installation.
- Stem Splitter's **managed local server and models** for songs that do not
  already contain the selected stem audio.
- Enough free disk space for Stem Splitter's one-time server/model installation.
  The download is several gigabytes.

The managed local server is simply a helper program that Stem Splitter starts
on your own computer. MinusMix does not send your song to a remote service.

### Set up the Stem Splitter server

If you are not sure whether a song already contains stems, start the server
before using MinusMix:

1. Open **Settings** in FeedBack and find **Stem Splitter**.
2. Find the **Local demucs server** section marked “recommended — start here.”
3. On first use, select **Install server + models (~5 GB)** and wait for it to
   finish. This is a one-time setup.
4. If it is already installed but stopped, select **Start server**.
5. Open MinusMix. Its status should say that the managed local Stem Splitter
   server is ready. Use **Refresh status** if needed.

The first installation and model warm-up can take a while. A supported NVIDIA
GPU makes separation much faster, but Stem Splitter can also run on the CPU.

MinusMix currently supports only this managed local server. Stem Splitter's
remote/custom servers, Docker server, and in-app engines are not used by
MinusMix. You do not need to configure any of those options.

## Install MinusMix

### Recommended: FeedBack Plugin Manager

1. Open **Plugins** in FeedBack's navigation.
2. Paste this address into the **Install Plugin** box:

   ```text
   https://github.com/vo90/feedBack-plugin-minus-mix.git
   ```

3. Select **Install**.
4. When installation succeeds, close and restart the FeedBack desktop app.
5. Open **MinusMix** under **Tools**.

This method requires Git on your computer because the Plugin Manager uses it to
download and update the plugin. If installation reports that Git is unavailable,
use the ZIP method below.

To update a Git installation later, open **Plugins**, select **Update** beside
MinusMix, and restart FeedBack when prompted.

### Alternative: install the ZIP

1. Download `MinusMix-<version>.zip` from the
   [latest MinusMix release](https://github.com/vo90/feedBack-plugin-minus-mix/releases/latest).
2. Close FeedBack.
3. Extract the ZIP into FeedBack's `plugins` folder shown below.
4. Check that the finished path ends in `plugins/minus_mix/plugin.json`.
5. Restart FeedBack and open **MinusMix** under **Tools**.

| Platform | FeedBack plugins folder |
| --- | --- |
| Windows | `%APPDATA%\feedback-desktop\plugins` |
| macOS | `~/Library/Application Support/feedback-desktop/plugins` |
| Linux | `~/.config/feedback-desktop/plugins` or `$XDG_CONFIG_HOME/feedback-desktop/plugins` |

On Windows, press `Windows key + R`, paste
`%APPDATA%\feedback-desktop\plugins`, and press Enter to open the folder. Create
the `plugins` folder if it does not exist.

The ZIP already contains the `minus_mix` folder. Do not create another folder
around it. If the result is `plugins/minus_mix/minus_mix/plugin.json`, move the
inner `minus_mix` folder up one level.

ZIP installations cannot use the Plugin Manager's **Update** button. To update
one, close FeedBack and replace the existing `minus_mix` folder with the folder
from the new ZIP. MinusMix does not store your songs or exports inside its own
plugin folder.

## Create your first MinusMix song

For a simple first test, try one song and remove Guitar:

1. Start Stem Splitter's managed local server as described above.
2. Open **MinusMix** under **Tools**.
3. Stay on the **Single song** tab.
4. Under **Choose an original song**, select the song you want to practise.
5. Under **Audio to exclude**, select **Guitar**.
6. Under **Output folder**, select **Choose folder…** and choose where the new
   FeedPak should be saved. Your normal local FeedBack song folder is the
   simplest choice if you want the result to join that library.
7. Review the summary and select **Create MinusMix FeedPak**.
8. Leave FeedBack open while separation and export finish.

MinusMix reports each stage and shows the full path when the new file is ready.
The result will have a name similar to `My Song (No Guitar).feedpak`. Existing
files are never overwritten; a numbered filename is used if necessary.

Add the resulting FeedPak to your FeedBack library in the same way as any other
song package. If you saved it in a folder FeedBack already scans, refresh the
library if the new song does not appear immediately.

The result contains one newly rendered backing track. Charts are not removed,
so you can still choose the guitar arrangement and play it against the
guitar-free backing.

## Convert a folder of songs

After a successful single-song test, the **Batch folder** tab can process many
songs:

1. Choose a source folder containing FeedPaks.
2. Choose a different output folder.
3. Select the audio to exclude from every song.
4. Select **Scan folder** to preview what will happen.
5. Review the scan and select **Start batch**.

Batch work runs one song at a time for stability. One failed song does not stop
the rest of the queue. Sources remain read-only, completed outputs are kept if
you cancel, and existing files are not overwritten.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| “Managed local server is not running” | Open **Settings → Stem Splitter**, find **Local demucs server**, and select **Start server**. Return to MinusMix and select **Refresh status**. |
| Models are downloading or warming up | Wait for Stem Splitter to report that the models are ready. The first setup takes longer than later uses. |
| The server is busy | Wait for its current job to finish, then try again. Batch mode already runs one separation at a time. |
| Connection lost or separation interrupted | Restart the managed local server and retry. Your source song was not changed. |
| **Create MinusMix FeedPak** is disabled | Select a song, at least one instrument, and an output folder. If separation is needed, also make sure the server status is ready. |
| MinusMix cannot write to the output folder | Choose another existing folder that your user account can write to, such as a folder inside Documents. |
| A song is not listed | Make sure it is a local `.feedpak` or `.sloppak`, then select **Refresh** beside the song search. |
| Some instrument sound remains | AI separation is an estimate. Bleed is more likely when instruments overlap heavily in the original recording. |
| Guitar removal also changes another sound | The model produces one combined Guitar stem. Sounds classified into that stem are reduced together. |

If an export fails, the original FeedPak remains untouched. MinusMix publishes
the result only after the new package is complete, so a failed export does not
leave a partial final FeedPak.

## Common questions

### Does MinusMix change my original song?

No. The source is always read-only. MinusMix creates a new file in the output
folder you choose.

### Does it remove the chart too?

No. Selecting Guitar removes the estimated guitar **audio**, not the lead or
rhythm guitar arrangements. Charts, lyrics and artwork are copied to the new
FeedPak.

### Does it upload my music?

No. When separation is needed, MinusMix talks only to Stem Splitter's managed
server on the same computer. Configured remote/custom servers and API keys are
ignored.

### Do I always need the server?

No. If the song already contains the selected stem audio, MinusMix reuses it
and does not contact the server. If you are unsure, starting the server is the
simplest option.

### Can I remove more than one instrument?

Yes. Select any combination of Guitar, Bass, Drums, Vocals, Piano and Other.

### Can I safely cancel?

Yes. MinusMix stops at a safe checkpoint. Source songs remain untouched, and a
completed output that has already been published is kept.

## Technical details

For selected stems `S`, the rendered backing is:

```text
MinusMix output = original full mix - sum(S)
```

For an ordinary single-stem source, MinusMix calls the public HTTP API of Stem
Splitter's managed loopback server. It requests the selected stems into a
caller-owned temporary directory. The server may calculate all six sources
internally, but MinusMix downloads only recognised requested outputs and never
writes them into the source FeedPak.

If the source already contains the selected stems, MinusMix takes a server-free
fast path. Otherwise, requested audio is streamed into the temporary workspace
rather than buffered in memory. After the download, MinusMix asks the server to
delete that job's result cache; server TTL cleanup remains a fallback. The whole
temporary separation directory is deleted after export.

The subtraction happens on decoded audio in FFmpeg. The playable mix and its
optional preview are normally rendered together from one decode graph. An
independent preview fallback preserves compatibility without failing the main
export. The manifest is rewritten to one `full` stem, and the preview is rebuilt
from the new audio.

Every arrangement, lyric track, rig, cover and other non-stem asset is copied
into the new package. Applying MinusMix repeatedly to an already derived output
is discouraged because each generation includes another lossy audio encode.

### Safety contract

- Never edits, renames or deletes the source package.
- Never overwrites an output; collisions gain ` (2)`, ` (3)`, and so on.
- Builds a temporary archive in the destination and publishes it atomically.
- Rejects unsafe or duplicate archive paths and escaping directory symlinks.
- Verifies output-folder writability before separation or rendering begins.
- Accepts output-folder writes only from a loopback client.
- Sends separation requests only to Stem Splitter's managed loopback server.
- Uses the desktop shell's native directory chooser.

### Compatibility and server scope

- Compatible with the current main/nightly managed local Stem Splitter HTTP
  contract.
- Converting an unsplit FeedPak requires the managed local server to be running
  and ready.
- A FeedPak with the selected saved stems does not require the server.
- Remote/custom servers, Docker sidecars and Stem Splitter's in-app engines are
  outside the current MinusMix support scope.

### Batch implementation

Batch mode scans `.feedpak` and `.sloppak` files recursively. It can preserve
the source folder structure or place every generated FeedPak directly in one
output folder. Name collisions receive deterministic numbered names.

Separation runs sequentially to avoid GPU-memory contention. Byte-identical
full mixes in the same batch can reuse the requested temporary stem. The reuse
cache is deleted when the job finishes. Recent actionable job history is
bounded so large queues do not cause unbounded memory or status growth.

## Development

The full test suite requires Python 3.10 or newer, FFmpeg on `PATH`, and Node.js.
Install the development dependencies, then run the same checks used by CI:

```text
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
python scripts/build_release.py
```

The real-audio tests are skipped when FFmpeg is unavailable. Because those
tests contribute to the coverage floor, a run without FFmpeg is only a partial
check and may correctly finish below the required coverage percentage.

## License

MinusMix is available under the [MIT License](LICENSE).
