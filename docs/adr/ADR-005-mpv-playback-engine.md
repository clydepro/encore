# ADR-005: mpv Playback Engine

## Status

Accepted

## Date

2026-10-07

## Context

Encore must play MP3, FLAC, AAC and M4A gaplessly, on a Raspberry Pi 4, through
ALSA/PipeWire/PulseAudio configurations that the software cannot predict, and
it must keep playing when the controller process is briefly busy or when the
player itself dies. Building this on a decoder library means owning codecs,
output devices, gapless semantics and crash surface for the life of the project.

## Decision

Use **mpv** as the only audio engine, controlled over its **JSON IPC** socket.

- `encore/playback/` is the single owner of mpv: no HTTP, UI or queue code
  touches the socket (SAPRS 7.9).
- A **Playback Supervisor** launches mpv, establishes IPC, monitors the process,
  detects crashes, restarts, reconnects and reports health (SAPRS 7.2).
- The state machine is `Idle → Loading → Playing → (Paused) → Finished → Idle`
  with `Error → Recovering → Idle/Playing` for faults (SAPRS 7.3).
- Commands (play, pause, resume, stop, skip, seek, load) are asynchronous so the
  server never blocks on the audio path (SAPRS 7.4).
- Progress is sampled at about 1 Hz and published as immutable events
  (SAPRS 7.5, ADR-004).
- Gapless playback is delegated to mpv; crossfade is configured in the playback
  layer, never in a controller or template (SAPRS 7.8).
- Hardware assumptions live in installation configuration, not code (SAPRS 7.7).
- Recovery is a first-class feature: detect, record diagnostics, publish
  health/recovery events, restart, reconnect, restore state, continue the queue
  (SAPRS 7.6).

## Consequences

Positive:

- Codec support, output selection and gapless behaviour are maintained
  upstream by an engine that plays essentially everything.
- A crash costs one track, not the appliance, and is testable by killing a
  subprocess in a chaos test (SAPRS 14.13).
- Playback is isolated behind one interface, which keeps unit tests free of the
  real binary (SAPRS 14.4) using `tests/support/mpv.py`.

Costs:

- A runtime dependency that must be installed and versioned by the installer.
- A second process to supervise: IPC reconnect timing and zombie cleanup become
  explicit test cases.
- Feature availability is bounded by what mpv exposes; exotic DSP behaviour
  would need a different design.

## Alternatives considered

- **GStreamer via PyGObject.** Rejected: powerful, but the pipeline graph and
  platform plugin matrix are a larger maintenance surface than an mpv socket.
- **libmpv/Python decoding with a WAV writer.** Rejected: puts codec and
  gapless correctness inside Encore's bug budget.
- **SoX/FFmpeg spawned per track.** Rejected: no IPC, no pause/resume semantics,
  no gapless; process-per-track churn on a Pi 4.
- **PulseAudio/PipeWire-side queuing.** Rejected: moves the queue away from the
  component that must report it, and behaves differently per distribution.

## Implementation notes

The decision above is unchanged. These are the things building it taught us,
recorded here because each is a rule a later reader would otherwise rediscover,
and because none of them is visible from the mocked suite SAPRS 14.4 requires.

**mpv's replies are promises, not outcomes.** `loadfile` returns once the command
has been parsed; the file is opened some milliseconds later. A state machine that
treats an accepted command as a started track reads `idle-active=True` as "the
track ended" and publishes `SongFinished` for a song that never made a sound —
and a double that sets the properties inside the same call cannot show it.
`Loading` is therefore a state that must be able to last, bounded by a grace
period after which a file mpv accepted and never opened is reported as `FAILED`
rather than leaving the appliance stuck. Progress is sampled at roughly 1 Hz for
the same reason: the observation loop is what makes the engine's asynchrony
legible.

**Do not pass an option mpv may not know.** mpv treats an unrecognised
command-line option as fatal at parse time — it exits before opening the IPC
socket, which to an operator is "the appliance will not start". That rules out
newer flags as a portability strategy: `--input-ipc-run`, which would set the
socket's mode, is not recognised before 0.36, and passing it to 0.35.1 ended the
process. Socket permissions are Encore's to set instead — `0700` on the run
directory, `0600` on the socket once it appears, which is also the least-privilege
answer (AEP 17). No minimum mpv version is pinned here; that is the installer's
decision (SAPRS 7.7), and the code is written so that no version floor is needed.

**A channel owns a descriptor.** Whoever created the socket must be the one that
closes it, and a restart replaces a channel, so recovering by installing a new one
without closing the old leaks a descriptor per attempt. Nothing observable is wrong
until the process runs out.

**What this has still not proved.** Everything measured ran with `--ao=null`. The
decision's central assumption — that mpv reaches a speaker through whatever ALSA,
PipeWire or PulseAudio configuration the Pi has — remains untested, and belongs to
hardware bring-up rather than to this suite.
