# Voice Agent Runtime — Work Log & Backlog

## Completed Work

### 1. Dynamic Profiling

- `StreamEvents` in `main.py` reads the incoming `ControlSignal` before
  processing audio chunks.
- `system_prompt` from the agent profile YAML is extracted and passed
  directly into `LLMEngine.generate()` via `system_override`.
- Fallback system prompt in `engine.py` promoted to a named module-level
  constant instead of an anonymous inline string.
- Verified: LLM responds as the loaded agent persona, not as a generic
  assistant.

---

### 2. Session Management

- `session.go` rewritten from a passive data struct into an active
  controller owning the gRPC stream reference via `Attach()`.
- State machine enforced via a legal transition map protected by
  `sync.Mutex`. Illegal transitions log an error and no-op — they never
  panic or silently corrupt state.
- `writePump()` goroutine owns all outbound gRPC sends. Drains
  `UserAudioChan`, transitions to `PROCESSING` on exhaustion.
- `readPump()` goroutine owns all inbound gRPC receives. Transitions to
  `RESPONDING` on first audio chunk. Closes `AgentAudioChan` and
  `DoneChan` on EOF.
- `main.go` decoupled from gRPC entirely — feeds `UserAudioChan`,
  reads `AgentAudioChan`, has no knowledge of stream internals.
- `InterruptChan` allocated and reserved for future barge-in support.

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

### 4. True Duplex — Milestone 1: Three-Worker Architecture

- `main.py` rewritten from a sequential blocking handler into a
  three-worker concurrent architecture.
- `_read_pump` thread owns the inbound stream exclusively — no other
  thread touches the audio buffer, eliminating the need for a mutex on
  that resource through single-thread ownership.
- Per-utterance `_run_utterance` daemon threads dispatched on boundary
  signals — inference never blocks the listener.
- Main thread drains a `queue.Queue` with an `object()` sentinel
  (`_SHUTDOWN`) and yields to gRPC — the sole outbound path.

---

### True Duplex — Milestone 2: Single Utterance Duplex Path

[PR #6](https://github.com/Adityarya11/voice-agent-runtime/pull/6)

- `END_OF_UTTERANCE` added to `ControlSignal.SignalType` as value `3`
  in `agent.proto`. Proto regenerated on both Go and Python sides.
- `session.go` state machine updated: `ACTIVE → RESPONDING` direct
  transition added. `writePump` sends `END_OF_UTTERANCE` control signal
  instead of calling `CloseSend()`. Stream stays open after utterance
  boundary is signaled.
- `context.is_active()` guard added to `_run_utterance` — in-flight
  inference aborts cleanly when the gRPC context is cancelled mid-stream.
- Verified across two shutdown scenarios:
  - Python closed first: `_read_pump` catches `grpc.RpcError` on server
    drain, hits `finally`, puts `_SHUTDOWN` sentinel, exits cleanly.
  - Go closed first: `_read_pump` exhausts `request_iterator` naturally,
    hits `finally`, exits cleanly. Python server remains alive and accepts
    the next connection immediately.
- Confirmed: Go stream stays open after `END_OF_UTTERANCE`. Python
  detects boundary, dispatches inference on a daemon thread, streams
  response back while stream remains bidirectional. Go transitions
  `ACTIVE → RESPONDING` while the send side is still technically open.

---

### True Duplex — Milestone 3: Sequential Utterance Processing (completed)

[PR#8](https://github.com/Adityarya11/voice-agent-runtime/pull/8)

- `StreamUtterance()` added to `session.go` — streams a WAV file directly
  onto the gRPC stream and sends `END_OF_UTTERANCE` atomically after all
  audio bytes are sent. Eliminates the channel-drain race condition that
  caused audio bleeding between utterances when using `UserAudioChan` as
  an intermediary buffer.
- `UserAudioChan` and `writePump` removed from `Session` struct for this
  test harness — sending is now handled directly by `StreamUtterance`,
  preserving gRPC encapsulation without the concurrent-producer complexity
  that caused ordering bugs.
- `utterance_done_event` (`threading.Event`) added to `_read_pump` in
  `main.py` — gates dispatch of new `_run_utterance` threads so only one
  utterance is processed at a time. If `END_OF_UTTERANCE` arrives while
  inference is in progress, `_read_pump` blocks until the current thread
  completes before dispatching the next one.
- Verified with two distinct audio inputs on one open stream:
  - `input_1.wav` (440364 bytes) — earlier this caught halfway through the stt capture. and remaining bytes gor garbled in the next audio.
  - `input_2.wav` (391212 bytes) — Instead of starting from the actual, start of the audio 2, it started with the leftovers of the `input_1` and thus got garbled.
    - Solved using `utteranch_done_event`.
  - Both transcriptions correct, LLM responses coherent and distinct,
    zero byte bleeding between utterance buffers.
  - Sequential gate confirmed via "END_OF_UTTERANCE received while
    utterance in progress. Waiting" log line.

---

### True Duplex — Milestone 4: Polish and Edge Cases (Partial)

- Empty utterance buffer guard implemented and verified. Rogue `END_OF_UTTERANCE` control signals arriving before any audio chunks are caught and explicitly ignored, preventing downstream STT crashes or state corruption.
- Performance and latency benchmarks recorded under throttled hardware constraints (RTX 3050 Mobile):
  - **LLM TTFT (Time To First Token):** ~0.45s – 0.58s.
  - **TTS Generation:** ~2.12s cold start for the initial chunk; drops to ~0.06s – 0.10s for subsequent sentence chunks once the model is warm.
  - **STT (Faster-Whisper):** ~1.09s for initial audio; drops to ~0.63s on immediately subsequent utterances.

---

## Active Backlog

### True Duplex — Milestone 4: Polish and Edge Cases

**Priority:** Follows milestone 3.

- Backpressure: define behavior when `outbound_queue` grows beyond a
  threshold (inference faster than Go can consume — unlikely on current
  hardware but needs a defined policy).
  - Not Gonna happen as TTS Latency(s) > gRPC network call(ms).
- Verify `_run_utterance` temp file cleanup under all exit paths,
  including `context.is_active()` early return.

### VAD Integration — Milestone 1: Standalone Model Verification (completed)

- **[test_vad_standalone.py](../examples/test_vad_standalone.py)**
- Silero VAD ONNX model loaded via `onnxruntime`, downloaded directly rather
  than via the `silero-vad` package to keep dependencies minimal.
- Verified against `input_1.wav`: silence frames consistently score under
  0.1, speech frames consistently score above 0.5. Confirmed model input
  contract is a single unified `(2, 1, 128)` state tensor plus explicit
  `sr` input, not separate `h`/`c` tensors as initially assumed.
- Frame math verified: 512 samples at 16kHz = 32ms per frame.

### VAD Integration — Milestone 2: Four-State Debounce Machine

**Branch:** `feature/vad-integration`

- `vad/detector.py` implements `SILENCE -> SPEECH_STARTING -> SPEECH ->
SPEECH_ENDING` state machine over raw per-frame model output.
- Recurrent state (`state` tensor) persists across utterances within a
  session, reset only via explicit `reset()` at session boundaries -- not
  per utterance, since it encodes acoustic context rather than
  utterance-scoped information.
- Standalone test harness validates against tiled real audio segments
  (not synthetic frames, since the model does not respond predictably to
  synthetic signals).

### VAD Integration — Milestone 3: Pipeline Integration

- Wire `VADDetector` into `_read_pump`. Add `scipy.signal.resample_poly`
  to convert incoming gRPC audio to 16kHz before frame slicing.
- Go's `END_OF_UTTERANCE` send stays as a manual override during testing.
  Not removed yet.

### VAD Integration — Milestone 4: Tuning and Edge Cases

- Silence-only stream, false-start noise burst, back-to-back utterances
  with state persistence verified.
- Remove Go's explicit `END_OF_UTTERANCE` send once VAD is proven reliable
  end to end.

### Monitor Goroutine (Go)

**Priority:** High before AetherRTC integration.

A third goroutine inside `Session.Run()` watching for:

- Context cancellation (caller disconnects).
- Session timeout (inference stalls beyond a threshold).
- Signal on `InterruptChan` (barge-in: user speaks while AI is speaking).

On any of the above, the monitor must flush `AgentAudioChan`, cancel
the active `readPump` receive, and transition the session back to
`ACTIVE` to accept new audio.

### DoneChan sync.Once Guard

**Priority:** Medium. Implement alongside the monitor goroutine.

`DoneChan` is currently closed only by `readPump` on EOF — correct for
the happy path. Once the monitor goroutine can also signal session
completion (timeout, context cancel), multiple goroutines could race to
close `DoneChan`, which panics.

Fix: wrap `close(s.DoneChan)` in a `sync.Once` stored on the session
struct. Whichever goroutine reaches it first wins; subsequent calls are
no-ops.

### AetherRTC Integration

**Priority:** Follows VAD integration and monitor goroutine.

AetherRTC acts as the WebRTC edge gateway — terminates browser
connections, forces G.711/PCMU at the edge to avoid Opus CGO overhead,
and forwards raw PCM bytes into the Go orchestrator via an internal gRPC
bridge using the shared `agent.proto` contract.

AetherRTC state machine stays transport-only: `CONNECTING / STREAMING /
DISCONNECTED`. The Go orchestrator remains the sole state authority.
Split-brain is avoided by design — AetherRTC has no knowledge of
`PROCESSING`, `RESPONDING`, or any AI-layer state.

Integration is blocked on VAD being in place, since without VAD the
Python servicer has no mechanism to detect utterance boundaries from a
continuous live audio stream, and AetherRTC will never send
`END_OF_UTTERANCE` — it does not know what an utterance is.

### Concurrent Ordered Utterance Processing

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

---

## Reference Documents

- [`docs/HLD.md`](../docs/HLD.md) — High Level Design
- [`docs/LLD.md`](../docs/LLD.md) — Low Level Design
- [`docs/true_duplex.md`](../docs/true_duplex.md) — True Duplex
  implementation design, milestone breakdown, and architectural decisions
  _(to be written after milestone 4 completes)_
