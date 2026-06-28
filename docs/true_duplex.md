# True Duplex — Design, Implementation, and Milestones

## What this document is

A design record for the true duplex milestone of the Voice Agent Runtime.
It covers the architectural problem, the decisions made at each stage,
what was built, what was deliberately deferred, and what the system
looks like after all four milestones merged.

---

## The problem

The system prior to this work was half-duplex. The Go orchestrator read
an entire audio file, pushed all bytes onto the gRPC stream, called
`CloseSend()`, and then waited. Python's `StreamEvents` blocked on
`for event in request_iterator` until the iterator was exhausted —
which only happened when Go called `CloseSend()`. Only then did STT,
LLM, and TTS run. Only then did audio flow back.

This is a voicemail exchange, not a phone call. For a real voice agent:

- The user's audio stream never closes while the call is active.
- Inference must begin as soon as a speech boundary is detected, not
  when the caller hangs up.
- The agent must be able to stream a response while the caller is
  still potentially speaking.

AetherRTC, the companion WebRTC edge gateway, made this non-negotiable.
A live browser caller has no `CloseSend()` moment. Connecting AetherRTC
to the half-duplex system would result in Python waiting forever for an
iterator that never exhausts.

---

## The key design decisions

### Decision 1: Explicit boundary signal before VAD

The system needs a mechanism to tell Python "this utterance is complete,
start inference." Two options existed:

**Option A:** Voice Activity Detection (VAD) — detect silence in the
audio stream on the Python side and derive the boundary automatically.

**Option B:** Explicit `END_OF_UTTERANCE` control signal — Go sends a
typed signal when it considers the utterance complete.

Option B was chosen for the first implementation pass. Reason: building
the concurrent threading architecture and VAD simultaneously means two
new failure surfaces at once. If something breaks, the cause is
ambiguous. Option B has zero tuning surface — either Go sent the signal
and Python reacted, or it did not. Every bug is a concurrency bug, not
a VAD sensitivity bug. VAD replaces Option B's signal origin later
without changing the interface at all.

`END_OF_UTTERANCE` was added to `ControlSignal.SignalType` as value `3`
in `agent.proto`. Proto regenerated on both Go and Python sides.

### Decision 2: Three-worker Python architecture

Python's `StreamEvents` was rewritten from a sequential blocking function
into three concurrent execution contexts:

**`_read_pump` thread** — owns the inbound stream exclusively. No other
code ever touches the audio buffer. Single-thread ownership removes the
need for a mutex on the buffer entirely — there is nothing to synchronize
because only one thread ever reads or writes it. When `END_OF_UTTERANCE`
arrives, `_read_pump` copies the buffer bytes, clears the buffer, and
dispatches a `_run_utterance` thread. It never waits for inference to
finish — it immediately resumes listening.

**`_run_utterance` daemon threads** — one per utterance, spawned by
`_read_pump`. Each runs STT → LLM streaming → TTS per sentence, pushing
`Event` objects onto `outbound_queue` as audio is produced.

**Main thread (outbound relay)** — blocks on `outbound_queue.get()` and
yields events to gRPC. This is the only thread that can yield, satisfying
Python's generator requirement for gRPC streaming servicers.

The shared `queue.Queue` between `_run_utterance` and the main thread is
thread-safe by construction — Python's `Queue` implementation uses
internal locks. No additional synchronization needed.

The poison pill pattern — a singleton `_SHUTDOWN = object()` — signals
the main thread to exit its loop when `_read_pump` finishes. Using a
distinct object rather than `None` prevents accidental collision with any
legitimate queue value.

### Decision 3: Sequential utterance gating

Multiple `END_OF_UTTERANCE` signals can arrive while a previous inference
thread is still running. Without a gate, two `_run_utterance` threads
write interleaved audio chunks onto `outbound_queue`, producing garbled
output.

`threading.Event` was chosen over a mutex for the gate. A mutex would
require spin-polling to check completion. `threading.Event.wait()` uses
an OS-level condition variable — `_read_pump` sleeps at zero CPU cost
until the current `_run_utterance` signals completion. `utterance_done_event`
starts in the set state (ready), is cleared before each dispatch, and
set again in `_run_utterance`'s `finally` block — which runs regardless
of how the function exits (success, empty transcription, context cancel,
exception).

Concurrent ordered processing — sequence-numbered chunks, enforced output
ordering — was explicitly deferred. It is the correct production approach
when barge-in is in scope and utterance overlap becomes the normal path
rather than an edge case. For the current milestone, sequential processing
is correct and eliminates the ordering problem with minimal complexity.

### Decision 4: `StreamUtterance` replaces `writePump`

Early milestone three implementations used `UserAudioChan` and a
`writePump` goroutine to send audio from `main.go`. This produced a
class of drain-ordering bugs: the `select` loop in `writePump` could not
distinguish utterance one's tail bytes from utterance two's head bytes
when both were in the buffered channel simultaneously.

The correct fix was to eliminate the channel abstraction for the test
harness entirely. `StreamUtterance(audioPath string)` on the `Session`
struct opens a file, streams all bytes directly onto the gRPC stream in
4096-byte chunks, and sends `END_OF_UTTERANCE` atomically before
returning. Caller cannot send a second utterance until the first's
boundary signal is on the wire — the ordering is enforced by the
function's sequential execution, not by channel timing.

`UserAudioChan` and `writePump` are removed from the session for this
phase. When AetherRTC lands as a genuine concurrent audio producer,
the channel pattern returns — but it belongs in AetherRTC's bridge code,
not in the core session. The session's contract is now: send utterances
via `StreamUtterance`, receive responses via `AgentAudioChan`, wait for
completion via `DoneChan`.

### Decision 5: `sync.Once` on `DoneChan`

`DoneChan` is closed by `readPump` on stream EOF. When the monitor
goroutine is introduced, multiple goroutines can legitimately signal
session completion — EOF, timeout, context cancel, fatal error. Closing
a channel twice panics in Go.

`sync.Once` wraps `close(s.DoneChan)` in a `signalDone()` method.
Whichever goroutine calls it first closes the channel and the call
completes. Every subsequent call is a silent no-op. `sync.Once.Do`
additionally guarantees that the winning call completes before any
concurrent losing call returns — stronger than "only one runs."

---

## What was built, milestone by milestone

### Milestone 1: Three-worker architecture

- `main.py` rewritten with `_read_pump`, `_run_utterance`, and main
  thread outbound relay.
- `queue.Queue` with `_SHUTDOWN` sentinel established as the outbound
  handoff point.

### Milestone 2: Single utterance duplex path

- `END_OF_UTTERANCE` added to proto. Both sides regenerated.
- `session.go` state machine: `ACTIVE → RESPONDING` direct transition
  added. `writePump` sends `END_OF_UTTERANCE` instead of `CloseSend()`.
- `context.is_active()` guard in `_run_utterance` for clean abort on
  gRPC context cancellation.
- Verified: two shutdown scenarios (Python first, Go first) both handled
  cleanly. Stream stays open after boundary signal.

### Milestone 3: Sequential utterance processing

- `utterance_done_event` (`threading.Event`) gates `_run_utterance`
  dispatch.
- `StreamUtterance` replaces `UserAudioChan` + `writePump`.
- Verified: two distinct audio files, exact byte counts, zero bleeding,
  correct sequential responses.

### Milestone 4: Polish and edge cases

- Empty buffer guard: rogue `END_OF_UTTERANCE` before any audio is
  logged as a warning and ignored. No state corruption, no crash.
- Backpressure policy: `outbound_queue` is unbounded. Acceptable because
  TTS synthesis latency (seconds) exceeds gRPC send latency (milliseconds)
  on current hardware — the queue cannot grow faster than it is consumed
  in practice. Revisit if session duration or response length grows
  significantly.
- Temp file cleanup: `_run_utterance`'s `finally` block removes the temp
  WAV file under all exit paths including `context.is_active()` early
  return.
- `sync.Once` guard on `DoneChan` via `signalDone()` — safe for the
  monitor goroutine and any future concurrent completion sources.

---

## Measured performance (warm model, fresh boot, idle GPU)

Hardware: RTX 3050 Mobile 4GB VRAM, Ryzen 5 6600H, 16GB DDR5.

| Stage      | Metric                         | Value       |
| ---------- | ------------------------------ | ----------- |
| STT        | Transcription latency (warm)   | ~0.70–0.82s |
| LLM        | Time to first token (TTFT)     | ~0.43–0.58s |
| TTS        | First chunk (cold, Piper init) | ~2.1–5.0s   |
| TTS        | Subsequent chunks (warm)       | ~0.06–0.18s |
| End-to-end | STT → first audio byte         | ~1.0–1.1s   |

Cold-start notes: Piper pays a one-time ONNX graph initialization cost
on the first `synthesize()` call in a process. All subsequent calls
within the same process are warm. LLM TTFT is stable warm but can reach
~1.4s if Ollama has unloaded the model due to idle timeout — `keep_alive=-1`
mitigates this for long-running sessions but not across process restarts.

---

## What comes next

**VAD integration** — Silero VAD replaces the explicit `END_OF_UTTERANCE`
signal from Go. Python detects speech boundaries internally from the audio
stream. `_read_pump` fires the boundary event autonomously. Go never needs
to know when the user stops speaking.

**Monitor goroutine** — third goroutine in `Session.Run()` watching
context cancellation, timeout, and `InterruptChan` for barge-in support.
`sync.Once` on `DoneChan` makes this safe to add without panic risk.

**AetherRTC integration** — WebRTC edge gateway forwards live browser
audio into the orchestrator. Blocked on VAD (no boundary signal from
AetherRTC) and monitor goroutine (no barge-in handling).

See [`docs/backlog.md`](backlog.md) for full sequencing and priority.
