# Voice Agent Runtime — Work Log & Backlog

## Completed Work

### Dynamic Profiling

- `StreamEvents` in `main.py` reads the incoming `ControlSignal` before
  processing audio chunks.
- `system_prompt` from the agent profile YAML is extracted and passed
  directly into `LLMEngine.generate()` via `system_override`.
- Fallback system prompt in `engine.py` promoted to a named module-level
  constant instead of an anonymous inline string.
- Verified: LLM responds as the loaded agent persona, not as a generic
  assistant.

### Session Management

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

### Token Streaming from LLM

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

### True Duplex — Milestone 1: Three-Worker Architecture

- `main.py` rewritten from a sequential blocking handler into a
  three-worker concurrent architecture.
- `_read_pump` thread owns the inbound stream exclusively — no other
  thread touches the audio buffer, eliminating the need for a mutex on
  that resource through single-thread ownership.
- Per-utterance `_run_utterance` daemon threads dispatched on boundary
  signals — inference never blocks the listener.
- Main thread drains a `queue.Queue` with an `object()` sentinel
  (`_SHUTDOWN`) and yields to gRPC — the sole outbound path.

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

## Active Backlog

### True Duplex — Milestone 3: Sequential Utterance Processing

**Branch:** `feature/true-duplex-m3`
**Priority:** Current.

`_read_pump` currently spawns a new `_run_utterance` thread for every
`END_OF_UTTERANCE` signal with no gate on whether a previous utterance
is still being processed. If two utterances arrive in quick succession,
two inference threads write interleaved audio chunks onto the shared
queue, producing garbled output.

**Design decision:** Sequential processing via an in-progress flag rather
than concurrent ordered processing. Only one `_run_utterance` thread
runs at a time. If a boundary signal arrives while inference is in
progress, the new utterance audio is buffered and dispatched only after
the current thread completes. This eliminates the ordering problem with
zero coordination overhead and is the correct choice before VAD is in
place, since without VAD the boundary signals are synthetic and
overlapping utterances in a test harness are an edge case, not the
normal path.

Concurrent ordered processing — sequence-numbered chunks, write_pump
enforcing strict output order — is tracked as a future enhancement for
when real concurrent callers and barge-in behavior are in scope.

### True Duplex — Milestone 4: Polish and Edge Cases

**Priority:** Follows milestone 3.

- Graceful handling of empty utterance buffer on `END_OF_UTTERANCE`
  (already guarded with a warning log — verify behavior under test).
- Backpressure: define behavior when `outbound_queue` grows beyond a
  threshold (inference faster than Go can consume — unlikely on current
  hardware but needs a defined policy).
- Verify `_run_utterance` temp file cleanup under all exit paths,
  including `context.is_active()` early return.

### VAD Integration

**Priority:** Follows true duplex milestones.
**Branch:** new branch after milestone 4 merges.

`END_OF_UTTERANCE` is currently a synthetic boundary signal sent
explicitly by Go's test harness. For a live call via AetherRTC, no such
signal exists — the stream is continuous audio from a browser with no
`CloseSend()` and no explicit turn boundary.

VAD replaces the explicit signal with a detected one. Silero VAD running
on CPU inside `_read_pump` monitors the incoming audio stream and fires
a boundary event when it detects a sustained silence (tunable threshold,
starting at ~500ms). The `END_OF_UTTERANCE` enum value is reused or
repurposed — the signal's origin shifts from Go sending it explicitly to
Python detecting it internally and acting on it without any signal from
Go at all.

The `vad/` directory is already scaffolded in `inference-py/`.
Dependency: `silero-vad` via torch or onnxruntime — evaluate VRAM
impact before committing, since the GPU is already carrying `qwen2.5:3b`
at 2.2GB.

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
