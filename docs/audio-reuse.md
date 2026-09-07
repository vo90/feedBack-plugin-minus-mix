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

`reuse_batch` stores only compact inspected metadata and review bindings, not parsed
charts or audio. Scans are limited to 10,000 packages per input tree, with bounded
thread submissions. Ambiguous audio/preview hashes require a group choice. Invalid
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

Each output is built privately beside its destination. Publication uses an atomic
hard link or Windows MoveFileEx without replace permission. Filesystems that cannot
provide no-replace publication fail safely. There is no placeholder-and-replace
fallback. Cancellation removes only the operation's unpublished temporary file.

The current job checkpoint is atomically saved in the plugin configuration folder
at preview, choice, start and end. Completed/failed rows append small, flushed and
fsynced receipt records, so N outputs do not cause N full N-row checkpoint rewrites.
The checkpoint records the full reviewed row set and selected donor hashes; each
journal record binds its job, target and receipt to that plan. Restart replays the
valid journal prefix and reports a truncated/corrupt tail without trusting it.
A crash-published package can be recovered only after its entire expected manifest,
fresh non-audio members and donor audio/preview bytes are verified. Checkpoint shape
errors do not prevent plugin startup. Saved state is not sufficient authorization
to bypass current containment, source identity or content checks.

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
