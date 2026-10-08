# Audio reuse implementation

The feature is independent of the ordinary sequential separation queue. It builds
new packages under a third, non-overlapping root and never mutates either input.
The v0.6.2 per-song temporary-separation policy remains intact.

`reuse_match` reads bounded ZIP metadata and pitched JSON charts. Candidate discovery
uses normalized artist and base title. Final compatibility compares available
album/year, duration within 1 ms, normalized finite offsets and a sorted multiset
of musical chart fingerprints. Each fingerprint contains tuning, capo and the set
of sounding onset/string/fret tuples at millisecond precision. Chords expand to
their note entries or declared template. Pitchless mutes normalize their fret;
pitched frets remain exact. Ignored notes are omitted. At least eight events over
three onsets are required. Role names, bend/sustain fields, phrase ladders and
display/timeline bookkeeping are deliberately excluded because corrected converters
can change them. This is conservative version compatibility, not audio recognition.

Donor variants come from the validated `minus_mix.excluded_stems` metadata, using
the same canonical stem names and labels as normal MinusMix exports. A fresh song
can produce several output rows, one per compatible removed-stem set found in the
donor folder. Row/group identities and output names distinguish those sets. Audio
and preview duplicates collapse only within a set; competing recordings within
that set still require a choice. Missing or invalid variant metadata is reported
without inferring a variant from the filename. The UI shows input-package and
output-variant totals separately.

`reuse_batch` stores only compact inspected metadata and review bindings, not parsed
charts or audio. Scans are limited to 10,000 packages per input tree, with bounded
thread submissions. Expanded reviews are limited to 50,000 rows and must fit a
64 MiB saved checkpoint, including reserved space for completion receipts. Large
metadata can reach that size limit before the row limit; the preview then asks for
smaller input folders. Status pagination supports the full bounded row set.
Ambiguous audio/preview hashes within a variant require a group choice. Invalid
donors are visible but do not stop unrelated readable matches. Apply checks only
the approved files, without rescanning the input trees. Newly added alternatives
after preview do not change the approved source choice.

On Apply, current selected packages are inspected again and their full archive
hashes must match the preview. The temporary live fresh snapshot additionally
contains manifest and retained-member hashes. `reuse_export` checks those exact
inventories and hashes while streaming, plus the donor audio/preview hashes and
input signatures before/after. It verifies every output member before publication.
This avoids redundant whole-audio hashing between every packaging phase while
retaining cryptographic checks on all copied content. No decoding, resampling or
normalization occurs.

Variants using the same input share its inspected snapshot within one Apply run.
The completed-snapshot cache is limited to 32 entries and 8 MiB of serialized
metadata, and is cleared between runs. Each cached use rechecks the complete input
archive hash as well as its signature. Each output still checks guarded paths
and all streamed member hashes. The worker policy is unchanged;
this cache contains no decoded audio or separated stems.

Each output is built privately beside its destination. Publication uses an atomic
hard link or Windows MoveFileEx without replace permission. Filesystems that cannot
provide no-replace publication fail safely. There is no placeholder-and-replace
fallback. Cancellation removes only the operation's unpublished temporary file.

The current job checkpoint is atomically saved in the plugin configuration folder
at preview, choice, start and end. Completed/failed rows append small, flushed and
fsynced receipt records, so N outputs do not cause N full N-row checkpoint rewrites.
Each receipt is limited to 2 MiB, with space checked before approving the preview.
Terminal status becomes public only after the final save finishes; a new scan or
resume cannot overlap that save.
The checkpoint records the full reviewed row set and selected donor hashes; each
journal record binds its job, target and receipt to that plan. Restart replays the
valid journal prefix and reports a truncated/corrupt tail without trusting it.
A crash-published package can be recovered only after its entire expected manifest,
fresh non-audio members and donor audio/preview bytes are verified. Checkpoint shape
errors do not prevent plugin startup. Saved state is not sufficient authorization
to bypass current containment, source identity or content checks.

The mixed-variant policy does not reinterpret No Guitar-only checkpoints or their
journals. Those older saved previews require a fresh scan; their records are
retained, and existing outputs are never removed or overwritten by the upgrade.

Tests cover repair-tolerant matching, version mismatches, copied bytes, no-replace
races, stale inputs, invalid checkpoints, resource admission and restart/resume.
Performance measurements use disposable copied packages at 1, 2 and 4 workers.

The development benchmark used 32 package pairs made from copies of two real
songs, repeated at requested worker counts 1, 2, 4, 8 and 16. All 160 outputs
preserved fresh non-audio payloads and donor full/preview bytes; all input hashes
remained unchanged. Apply took 15.87, 13.55 and 10.36 seconds at 1, 2 and 4 effective
workers. Available memory capped requests 8 and 16 at 4 workers, while Auto chose 2.
These timings precede the receipt-journal optimization. A separate 300-package
test verifies bounded checkpoint writes, journal replay and byte-exact resume.

An existing complete conversion of “38 Special — Caught Up in You” made by the
corrected FeedForge converter also matches its older No Guitar package across all
three pitched arrangements, despite the repaired bend representations.
