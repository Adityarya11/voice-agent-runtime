# VAD Integration — Design, Implementation, and Milestones

## What this document is

A design record for the VAD integration milestone. Covers the problem,
key decisions, what was built and tested at each stage, and what was
deliberately deferred.

---

## The problem

Prior to this work, utterance boundaries were signaled explicitly by Go
via a `ControlSignal` of type `END_OF_UTTERANCE`. This was correct for
proving the duplex threading model, but it does not generalize: a live
caller has no mechanism to signal "I am done speaking." AetherRTC, once
integrated, will never send this signal — it forwards raw audio and has
no concept of an utterance at all.

VAD replaces the explicit signal with a detected one. Python listens to
the continuous audio stream and derives speech boundaries internally.

---

## Key design decisions

### Model and runtime

Silero VAD via ONNX, run through `onnxruntime` directly rather than the
`silero-vad` PyPI wrapper. `onnxruntime` was already a dependency;
downloading the ONNX file directly kept the dependency footprint minimal
and forced explicit understanding of the model's actual I/O contract
rather than trusting a wrapper library's abstraction of it.

The model's real input signature — discovered by testing against the
actual file rather than trusting documentation from memory — is a single
unified `state` tensor of shape `(2, 1, 128)` plus an explicit `sr` input,
not separate `h`/`c` tensors as initially assumed.

### Four-state debounce machine

Raw per-frame speech probability has no concept of an utterance boundary.
`SILENCE -> SPEECH_STARTING -> SPEECH -> SPEECH_ENDING` sits on top of it:

- `SPEECH_STARTING` requires `min_speech_duration_ms` of continuous
  speech-scoring frames before confirming an utterance start, rejecting
  transient noise (coughs, mic bumps).
- `SPEECH_ENDING` requires `min_silence_duration_ms` of continuous
  silence before firing the boundary, tolerating natural mid-sentence
  pauses without prematurely ending the utterance.

### RNN state persists across utterances, resets only at session boundaries

The recurrent state encodes short-term acoustic context — noise floor,
room characteristics — not utterance-scoped information. Resetting it
at every utterance boundary would force the model to re-establish
acoustic context from zero on every turn, degrading quality for no
benefit. `reset()` exists for session start or hard reconnect only.

### Lookback buffer

VAD is reactive — it must observe `min_speech_duration_ms` of speech
before confirming a start, by which point the first phonemes have
already passed. A `collections.deque` of the trailing ~320ms is
maintained continuously during `SILENCE` and folded into the utterance
buffer the moment speech is confirmed, so the confirmed utterance
includes audio from before the debounce threshold was crossed.

### Max utterance duration ceiling

Unbounded `SPEECH`/`SPEECH_ENDING` accumulation is an OOM risk if a
session holds an open microphone in a noisy environment indefinitely.
A configurable `max_utterance_sec` (default 15s) forces a boundary
regardless of the silence debounce. This is a memory safety ceiling,
not a linguistic one — it will cut a genuinely long uninterrupted answer
mid-sentence if one runs past the limit. Accepted tradeoff for this
milestone.

### Trailing silence retained, not trimmed

`get_utterance_frames()` includes the trailing silence accumulated
during `SPEECH_ENDING`, up to `min_silence_duration_ms`. Whisper
tolerates trailing silence without issue; trimming it precisely adds
complexity for no measurable accuracy gain. Revisited only if evidence
emerges that it matters in practice.

### Preprocessor and detector are separate concerns

`vad/preprocessor.py` (`AudioPreprocessor`) owns byte-to-frame
conversion: raw PCM bytes to int16 to normalized float32 to resampled
16kHz to fixed 512-sample frames, carrying remainder across calls.
`vad/detector.py` (`VADDetector`) owns only the debounce state machine
and ONNX inference. Neither knows about the other's internals. This
keeps the detector testable with pure numpy arrays with no gRPC or byte
handling involved.

### Per-session instantiation

`VADDetector` and `AudioPreprocessor` are instantiated fresh inside
`_read_pump` for each `StreamEvents` call, not shared at the servicer
level. Both carry state scoped to one continuous audio stream; sharing
an instance across sessions would leak one caller's acoustic context
and debounce position into another's.

---

## What was built and tested, milestone by milestone

### Milestone 1: Standalone model verification

Confirmed ONNX loads, accepts the real `(2, 1, 128)` state contract,
and produces sane scores on real recorded audio (silence consistently
under 0.1, speech consistently over 0.5).

### Milestone 2: Four-state debounce machine

Built and validated `vad/detector.py` against real audio -- a tiling
approach using looped real segments was attempted first and rejected,
since splice points introduced transients the model read as false
speech onsets. Validated instead using an exact digital silence
injection between two real speech segments (isolating the debounce
timing from acoustic ambiguity) and a live human recording (which
proved the detector's correctness by rejecting an intended sub-500ms
pause that measured closer to 1100ms in practice).

### Milestone 3: Pipeline integration

Wired `AudioPreprocessor` and `VADDetector` into `_read_pump`. Verified
end to end against the existing two-utterance Go test harness: VAD
independently detected both utterance boundaries, correct transcriptions
and responses for both, sequential gating held. Go's explicit
`END_OF_UTTERANCE` signal retained as a manual override during testing,
not yet removed.

### Milestone 4: Lookback, max duration, and edge case verification

Added the lookback buffer and max-duration ceiling described above.
Verified against three isolated test scripts:

- **False start**: a short noise burst below `min_speech_duration_ms`
  correctly produces zero `START_SPEECH` and zero `END_OF_UTTERANCE`.
- **The ramble**: sustained speech scoring past `max_utterance_sec`
  correctly forces exactly one `END_OF_UTTERANCE` at the expected frame
  index. This test mocks the inference score directly and does not
  exercise genuine evolving RNN state across 200 frames -- it validates
  the frame-counting ceiling logic in isolation, not model behavior
  over a long real utterance.
- **State persistence**: confirmed the identical audio frame produces a
  different model score depending on whether it follows a fresh,
  zeroed detector or one carrying a prior utterance's context forward
  -- proof that state is not reset at the utterance boundary and is
  genuinely influencing inference, not merely present and unused.

---

## Known limitation carried forward

`_read_pump` processes one gRPC event at a time. When an utterance is
dispatched, `utterance_done_event.wait()` blocks the entire read loop
until inference completes -- including consumption of any new audio
arriving mid-inference. VAD is not actively evaluating incoming audio
during this window; anything spoken while a previous utterance is being
processed queues at the transport layer and is only evaluated once the
prior inference releases the lock. This is the same sequential-gating
tradeoff accepted in true-duplex milestone 3, carried forward
deliberately rather than solved here. It becomes the concrete motivation
for the next milestone.

---

## What comes next

**Barge-in** — the current system cannot detect or react to a caller
speaking while the agent is still generating or playing a response.
Solving this requires the sequential gating above to be replaced with
genuine concurrent evaluation, and a cancellation path from `_read_pump`
into an in-flight `_run_utterance` thread. This is the next architectural
milestone and depends on the monitor goroutine already scaffolded on the
Go side.

See [`docs/backlog.md`](backlog.md) for sequencing and priority.
