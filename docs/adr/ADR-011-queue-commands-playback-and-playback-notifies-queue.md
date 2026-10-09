# ADR-011: The Queue Commands Playback; Playback Notifies the Queue

## Status

Accepted

## Date

2026-07-29

## Context

Milestones 8 and 9 (AIG 21) put two services next to each other with a shared job: a song
ends, and the next one starts. AIG 4 says services communicate through interfaces or the
Event Bus, and AEP 19 asks that any direct service-to-service call be justified rather than
assumed. The queue and the player are the first pair where that rule actually bites.

The naive reading of "everything through the bus" produces a queue that publishes
`PlayThisSong`, subscribes to its own event, and calls mpv from the handler. That is not
decoupling; it is indirection with an extra step, and it is worse in two specific ways:

- An event is a **fact that already happened** (ADR-004). "Play this" is not a fact. Making a
  command look like a fact means the publisher cannot know whether it worked, so
  `QueueService.enqueue()` cannot tell a guest their request is now playing — it has to guess
  from whichever `SongStarted` event arrives next, which may belong to somebody else.
- Recovery becomes circular. When mpv dies, the queue must not start anything until the
  engine is back; if the only way for the queue to learn that is an event, and the only way
  to start a song is another event, then somebody has to own the retry — and the bus has no
  memory, no ordering across causes and no way to say "not yet".

The other naive reading — one service that owns both the queue and the player — is what the
SAPRS Chapter 7 and Chapter 8 separation exists to prevent, and AIG 7 lists them as two
services for the same reason.

## Decision

`QueueService` calls the playback engine **directly**, through a narrow `Player` protocol it
declares itself. Playback tells the queue about everything it did through the Event Bus.

Commands, queue to player:

| Queue action | Player method | Why it is a command |
| ------------ | ------------- | ------------------- |
| Accept a request while idle | `play(song, queue_item_id=…)` | SAPRS 8.2's "begin playback" happens *because* of the request |
| Administrative skip | `skip()` | The administrator asked for the current track to end now |
| Administrative stop | `stop()` | Same, with the track returned to the head |
| Pause / resume | `pause()` / `resume()` | SAPRS 7.4's commands, forwarded without opinion |

Facts, player to queue, on the bus:

| Event | Published by | Queue's reaction |
| ----- | ------------ | ---------------- |
| `SongStarted` | `PlaybackService` | Nothing. It is what the UI waits for. |
| `SongFinished` | `PlaybackService` | SAPRS 8.7's steps 2-5: settle, record, select, begin |
| `PlaybackRecovered` | `PlaybackSupervisor` | Begin the head item if the engine is idle |

Three rules keep the asymmetry from rotting into a mesh:

1. **The dependency is one-way.** `encore/playback/` does not import `encore/services/`, does
   not import the repositories, and does not know a queue exists. The track's identity comes
   in as an argument (`queue_item_id`) and goes back out unchanged, on the event — which is
   how the queue knows *which* of its items ended without the player holding a reference to
   it. `tests/unit/test_architecture_guardrails.py` enforces both halves.
2. **One fact, one publisher.** The player publishes `SongFinished` for every ending,
   including the endings it caused by a command from the queue. The queue never publishes a
   fact about playback it did not perform, and it never settles a queue item on its own
   authority: `skip()` asks, and the resulting event does the work. A command that both acted
   and announced would be two paths through SAPRS 8.7, which is how a queue ends up with two
   rows for one song.
3. **The protocol is the seam.** `Player` is five methods and three properties, declared in
   `encore/services/queue_service.py` — not an import of `encore.playback`. A unit test of
   queue rules needs a recorder, not a player; a test of playback needs neither.
4. **The service and the monitor bind after construction.** A crash is reported *to*
   `PlaybackService`, and the service asks `PlaybackSupervisor` to recover — so each needs
   the other, which two constructors cannot express. `supervisor.bind(playback)` is the
   seam, and it exists so the composition root does not assign a private attribute from
   outside the package. Ordering inside that cycle is fixed by rule 2: the service publishes
   the track's `SongFinished` first, then asks for recovery, so a queue waking on
   `PlaybackRecovered` finds an idle engine and not a track it believes is still playing.

Two consequences of rule 2 needed their own decisions, and are recorded here because they are
surprising in the code:

- **`SongFinished` carries `completion`.** History stores how much of a track was heard
  (SAPRS 5.7), and only the player knows. The field defaults to `0.0`, so the eight-event
  vocabulary is unchanged and every existing publisher stays valid.
- **Advancement is a loop, not a recursion.** A track that mpv cannot open produces
  `SongFinished(FAILED)` from *inside* `play()`, which re-enters the queue's handler. The
  handler settles the item and returns, and the outer loop picks the next head. Recursing
  would nest one delivery per unreadable file until `EventBus` reported an event cycle — so a
  shelf of corrupted files would end the party with a traceback instead of a skipped track.

## Alternatives Considered

**An event for "play this song".** Rejected: it is a command wearing a fact's clothes, and it
costs the queue its return value. `enqueue()` must answer "you are number 3" or "you are
playing now" in the same HTTP request, and an event stream cannot answer that question about
a call that has not happened yet.

**The queue subscribes to nothing and polls the player.** Rejected: SAPRS 8.7 defines the
advancement as a response to a track ending, and polling adds the latency budget SAPRS 1.9
spends elsewhere. It would also mean the queue, not the player, decides what "finished" means
— the coupling AIG 4 forbids, arriving through the back door.

**One service owning both.** Rejected: AIG 7 lists them separately, and the unit-test rule in
SAPRS 14.4 (no mpv required) would be violated the moment queue logic needed an engine. It
also merges two state machines that disagree: SAPRS 7.3's states and SAPRS 8.1's ordering are
different axes.

**Both directions on the bus, with a `PlaybackRequested` event and a reply channel.**
Rejected: the bus is in-process and synchronous (ADR-004), so a reply channel is a
`Future` — which is the direct call again, with a registry of pending ids to leak.

## Consequences

**Positive**

- Queue rules are testable with a recorder standing in for the engine, so SAPRS 8's twelve
  requirements are unit-tested in milliseconds without mpv, real or mocked.
- Recovery needs no new vocabulary: the queue already listens for `PlaybackRecovered`, and
  the supervisor does not need to know a queue exists.
- There is exactly one path that settles a queue item and writes history — the `SongFinished`
  handler — which is what makes the "one publication per track" guard in
  `test_playback_service.py` meaningful.

**Negative**

- The exception has to be maintained. A `grep` for `encore.playback` inside
  `encore/services/` is a guardrail rather than a convention, and if the queue ever grows a
  second direct dependency this ADR has to say why that one and not the bus.
- `queue_item_id` now travels through three layers (`QueueService` → `Player` →
  `PlaybackService` → event). It is a correlation id and nothing more; if anyone begins
  reading it in playback, playback has started knowing about the queue and the rule is broken.
- The queue's behaviour depends on an event being delivered synchronously. That is ADR-004's
  contract, and this ADR leans on it harder than anything else does: an asynchronous bus would
  make `enqueue()` return before the track had started, and the "you are playing now" answer
  would be wrong.

## Verification

- `tests/unit/test_queue_service.py` — SAPRS 8.1-8.8 against a recording player, including the
  re-entrancy limit and the "commands cost no events" rule for pause and resume.
- `tests/unit/test_playback_service.py` — one `SongFinished` per track whatever races with it;
  `queue_item_id` preserved across a command.
- `tests/integration/test_queue_playback_and_search.py` — the pair over both real databases:
  crash mid-set, unreadable file, rebuilt library, process restart.
- `tests/unit/test_architecture_guardrails.py` — playback imports neither services nor
  repositories; `encore/services/` imports neither `encore.playback` nor a storage driver.
- `tests/regression/test_issue_23_search_playback_queue.py` — four of the five failures this
  split caused before it was drawn that way.

## Related

ADR-004 (the bus, and what an event is), ADR-005 (mpv as the engine, and why commands are
synchronous), ADR-009 (the two databases the queue writes to and the one it reads), ADR-010
(the Builder that can make a queued song stop existing). SAPRS Chapters 7, 8 and 14.10.
