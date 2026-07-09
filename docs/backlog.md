# Voice Agent Runtime — Work Log & Backlog

## Completed Work

### 1. Dynamic Profiling

- `StreamEvents` in `main.py` reads the incoming `ControlSignal` before processing audio chunks.
- `system_prompt` from the agent profile YAML is extracted and passed directly into `LLMEngine.generate()` via `system_override`.
- Fallback system prompt in `engine.py` promoted to a named module-level constant instead of an anonymous inline string.
- Verified: LLM responds as the loaded agent persona, not as a generic assistant.

---

### 2. Session Management

- `session.go` rewritten from a passive data struct into an active controller owning the gRPC stream reference via `Attach()`.
- State machine enforced via a legal transition map protected by `sync.Mutex`. Illegal transitions log an error and no-op — they never panic or silently corrupt state.
- `writePump()` goroutine owns all outbound gRPC sends. Drains `UserAudioChan`, transitions to `PROCESSING` on exhaustion.
- `readPump()` goroutine owns all inbound gRPC receives. Transitions to `RESPONDING` on first audio chunk. Closes `AgentAudioChan` and `DoneChan` on EOF.
- `main.go` decoupled from gRPC entirely - feeds `UserAudioChan`, reads `AgentAudioChan`, has no knowledge of stream internals.
- `InterruptChan` allocated and reserved for future barge-in support.
- `DoneChan` closure hardened with `sync.Once` via a `signalDone()` method - safe against multiple goroutines racing to signal session completion once the monitor goroutine lands.

---

### 3. Token Streaming from LLM

- `generate_stream()` introduced with `stream=True` and `keep_alive=-1`
  to eliminate cold-start overhead and enable incremental delivery.
- STT language pinned to English, bypassing autodetection and reducing
  transcription latency by ~0.67s in measured runs.
- Sentence-boundary chunking with minimum length guard — TTS begins
  synthesizing the first sentence while the LLM is still generating later
  sentences.
- Per-stage latency instrumentation added: STT latency, TTFT, per-chunk
  TTS latency, total session response time.
- Measured steady-state performance (warm model, fresh boot, idle GPU):
  TTFT ~0.43–0.50s, end-to-end STT → first audio byte ~1.0–1.1s.

---

### 4. True Duplex (all 4 milestones completed)

Full design record, decisions, and milestone breakdown documented in
[`docs/true_duplex.md`](true_duplex.md). Summary:

- Three-worker Python architecture (`_read_pump`, `_run_utterance`,
  outbound relay) replacing the sequential blocking handler.
- `END_OF_UTTERANCE` control signal added to proto; stream stays
  bidirectionally open across the boundary instead of using `CloseSend()`.
- `StreamUtterance()` on `session.go` sends a full utterance and its
  boundary signal atomically, eliminating the channel-drain race that
  caused audio bleeding between utterances.
- `utterance_done_event` (`threading.Event`) gates sequential utterance
  dispatch — verified with two distinct audio inputs on one open stream,
  zero byte bleeding, correct and distinct responses.
- Empty buffer guard, backpressure policy (deferred — TTS latency
  exceeds gRPC send latency on current hardware), and temp file cleanup
  verified as milestone 4.

---

### 5. VAD Integration (Milestones 1–4 completed)

Full design record documented in [`docs/vad.md`](vad.md). Summary:

- Silero VAD via ONNX (`onnxruntime` directly, not the `silero-vad`
  wrapper). Real model contract confirmed: unified `(2, 1, 128)` state
  tensor plus explicit `sr` input.
- Four-state debounce machine (`SILENCE -> SPEECH_STARTING -> SPEECH ->
SPEECH_ENDING`) in `vad/detector.py`, validated against real recorded
  audio after a tiling-based synthetic test method was tried and
  rejected for introducing false transients.
- `vad/preprocessor.py` and `vad/audio.py` handle byte-to-frame
  conversion and frame-to-WAV conversion respectively, keeping
  `VADDetector` a pure state machine with no gRPC or byte-handling
  knowledge.
- `_read_pump` instantiates `VADDetector` and `AudioPreprocessor` fresh
  per session — both are stateful and must not be shared across calls.
- Lookback buffer (`collections.deque`, ~320ms) added to capture the
  phonetic onset of speech that occurs before the debounce threshold
  confirms an utterance start.
- Max utterance duration ceiling (`max_utterance_sec`, default 15s)
  added as an OOM safeguard against unbounded speech accumulation.
- Verified against three isolated test scripts: false start (noise
  burst below `min_speech_duration_ms` correctly produces zero
  boundaries), the ramble (sustained speech past `max_utterance_sec`
  correctly forces one boundary at the expected frame index), and state
  persistence (identical audio frame scores differently depending on
  prior context, proving RNN state is not reset between utterances and
  is genuinely influencing inference).
- **Not yet done:** running the full pipeline end-to-end with Go's
  manual `END_OF_UTTERANCE` override removed, confirming VAD alone
  drives utterance boundaries in a live two-utterance session. Tracked
  below.

---

## Active Backlog

### 1. VAD: Sever the Manual Override

**Priority:** High. Next task.

`vad/detector.py` is fully validated in isolation, but the full pipeline
has not yet been run with Go's explicit signal removed. `StreamUtterance`
in `session.go` currently streams audio and sends `END_OF_UTTERANCE`
atomically as one unit — there are no separate lines to comment out in
`cmd/main.go`; the signal is baked into the method itself. Severing it
requires either a new `StreamAudioOnly` method or a boolean parameter on
`StreamUtterance` to suppress the trailing signal.

Critically: an idle gap between two audio-only streams (via sleep or
otherwise) produces no frames for VAD to evaluate — silence must be
detected by the debounce machine, not inferred from elapsed time. To
prove VAD alone can separate two utterances, the test harness must
stream a real block of zeroed PCM samples (silence) between `input_1.wav`
and `input_2.wav`, exceeding `min_silence_duration_ms` (500ms), so the
detector has real audio to transition `SPEECH_ENDING -> SILENCE` against.

Once verified end to end with zero signals from Go, decide whether to
delete the manual override path from `main.py` entirely or retain it as
a documented debug/testing affordance.

### 2. AetherRTC Integration

**Priority:** High. Can proceed in parallel with or immediately after
the VAD override test above. Not blocked by the monitor goroutine —
barge-in specifically depends on it, basic bidirectional audio routing
does not.

AetherRTC acts as the WebRTC edge gateway — terminates browser
connections, forces G.711/PCMU at the edge to avoid Opus CGO overhead,
and forwards raw PCM bytes into the Go orchestrator via an internal gRPC
bridge using the shared `agent.proto` contract.

AetherRTC state machine stays transport-only: `CONNECTING / STREAMING /
DISCONNECTED`. The Go orchestrator remains the sole state authority.
Split-brain is avoided by design — AetherRTC has no knowledge of
`PROCESSING`, `RESPONDING`, or any AI-layer state.

With VAD in place, the Python servicer no longer needs Go to send an
explicit utterance boundary — it can derive boundaries from AetherRTC's
continuous live audio stream directly. Open item before wiring this up:
`SOURCE_SAMPLE_RATE` in `main.py` is currently a hardcoded 44100 constant
matching the test harness's WAV files. AetherRTC forwards 8kHz G.711
audio, so this needs to become either a per-session parameter negotiated
at `START_SESSION`, or AetherRTC resamples before the gRPC bridge.

Project has been migrated from GitHub to GitLab; README's companion
project link needs updating once the new location is finalized.

### 3. Monitor Goroutine (Go)

**Priority:** Medium. Deferred until a single-user end-to-end path
(VAD alone + AetherRTC basic routing) is fully verified. Only barge-in
requires this — nothing in basic AetherRTC audio routing does.

A third goroutine inside `Session.Run()` watching for:

- Context cancellation (caller disconnects).
- Session timeout (inference stalls beyond a threshold).
- Signal on `InterruptChan` (barge-in: user speaks while AI is speaking).

On any of the above, the monitor must flush `AgentAudioChan`, cancel
the active `readPump` receive, and transition the session back to
`ACTIVE` to accept new audio.

### 4. Concurrent Ordered Utterance Processing

**Priority:** Low. Future enhancement, not current scope.

When multiple overlapping utterances need to be processed concurrently
without serialization, the queue-based ordering approach requires:

- Sequence numbers assigned per utterance at boundary detection time.
- `write_pump` holding back out-of-order chunks and releasing them
  in strict sequence number order.
- A defined policy for what happens if utterance N+1 completes before
  utterance N (discard N+1, or buffer and release after N).

This becomes relevant when barge-in is in scope and real-time
responsiveness to overlapping speech matters more than strict
serialization.

### Phase 2: Extensibility and Tool Calling (deferred, tracked only)

**Priority:** Not scoped yet. Explicitly out of Phase 1.

Tool calling, third-party integrations (Gmail, database-backed user
state), and broader agent extensibility are intentionally deferred to a
dedicated design discussion once Phase 1 (single-user, VAD-driven,
AetherRTC-connected voice pipeline) is complete and stable. Noted here
so the intent isn't lost, not to be expanded until that discussion
happens.

---

## Reference Documents

- [`docs/HLD.md`](HLD.md) — High Level Design
- [`docs/LLD.md`](LLD.md) — Low Level Design
- [`docs/true_duplex.md`](true_duplex.md) — True Duplex implementation
  design, milestone breakdown, and architectural decisions
- [`docs/vad.md`](vad.md) — VAD integration design, milestone breakdown,
  and architectural decisions
