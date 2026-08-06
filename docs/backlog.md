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
- `main.go` decoupled from gRPC entirely — feeds `UserAudioChan`, reads `AgentAudioChan`, has no knowledge of stream internals.
- `InterruptChan` allocated and reserved for future barge-in support.
- `DoneChan` closure hardened with `sync.Once` via a `signalDone()` method — safe against multiple goroutines racing to signal session completion once the monitor goroutine lands.

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

### → VAD: Sever the Manual Override (completed)

`main.go`'s test harness no longer sends any `END_OF_UTTERANCE` control
signal. `StreamAudio` (audio only, no boundary) and `StreamSilence`
(explicit zeroed PCM injection) added to `session.go`, factored through
a shared `sendAudioChunk` helper alongside the existing `StreamUtterance`
— no duplicated chunking logic, no test-only flags added to the
production session API.

One real bug surfaced during this test: root cause was `stream.CloseSend()`
called immediately after the second silence injection, which caused
`_read_pump`'s `finally` block to unconditionally shut down the outbound
queue the moment inbound input ended, without checking for an in-flight
inference thread. Removing `CloseSend()` resolved it.

Known limitation carried forward: `_read_pump` does not currently support
half-close — input ending and response completion are not independently
tracked. Revisit alongside the monitor goroutine.

---

### 6. AetherRTC Integration (Milestones 1–5 completed)

Full architecture record in [`docs/three-tier-architecture.md`](three-tier-architecture.md).

**Topology:**

```
Browser <--WebRTC--> AetherRTC <--gRPC--> Orchestrator-Go <--gRPC--> Inference-Python
                      (gateway.proto)                (agent.proto)
```

**Milestone 1 — Proto contracts** ✅

- `gateway.proto` finalized with `oneof GatewayEvent { AudioChunk audio; GatewayControl control; }`.
- `int32 source_sample_rate = 3` added to `agent.proto`'s `ControlSignal` — purely additive.
- Both repos compile with new fields; independent codegen, no shared bindings between repos.

**Milestone 2 — Orchestrator-Go: Gateway server skeleton** ✅

- `internal/gateway/server.go` implements `Gateway` service on `:50052`.
- Reads `GatewayControl{START_SESSION, source_sample_rate}` as first event.
- Creates `Session` via existing `NewSession`, attaches to fresh `agent.proto` stream.
- Verified via throwaway test client.

**Milestone 3 — Orchestrator-Go: bidirectional bridge** ✅

- Inbound relay: `GatewayEvent{AudioChunk}` → forwarded as `agent.proto` `Event{AudioChunk}` to Python.
- Outbound relay: goroutine draining `AgentAudioChan` → `GatewayEvent{AudioChunk}` back to AetherRTC.
- `cancelAgent()` context cancellation prevents shutdown deadlock when AetherRTC disconnects first.
- `END_SESSION` handling closes session cleanly.
- Verified: test client streaming real WAV bytes produced correct STT/LLM/TTS cycle.

**Milestone 4 — AetherRTC: bridge client** ✅

- `internal/bridge/client.go` — single shared `grpc.ClientConn` to Orchestrator-Go, one `StreamAudio` per session.
- `internal/bridge/stream_manager.go` — drains `PCMInboundChan`, sends `AudioChunk`s, receives reverse stream onto `PCMOutboundChan`.
- Wired into `signaling/server.go` after `ProcessOffer` succeeds.
- Verified: real browser tab produced correct STT/LLM/TTS cycle in Python logs — live mic audio end to end.

**Bugs discovered and fixed during Milestone 4 live testing:**

- `SOURCE_SAMPLE_RATE` was hardcoded to 44100 in `main.py` regardless of negotiated rate. Fixed: defer `AudioPreprocessor` construction until `START_SESSION` control event is received, using `control.source_sample_rate`.
- `AudioPreprocessor.push()` resampled each 20ms RTP packet independently, causing ramp-up/ramp-down filter artifacts at every packet boundary. Fixed: buffer raw samples and resample in 100ms blocks, yielding frames from the combined output.
- `_read_pump` blocked on `utterance_done_event.wait()` while a previous utterance was still being processed, freezing gRPC inbound read, backpressuring Go, filling `PCMInboundChan`, and silently dropping incoming audio. Fixed: decoupled VAD ingest from inference dispatch via `utterance_queue` and `_utterance_dispatcher` thread — read loop never blocks.
- VAD pause tolerance raised to 800ms to reduce premature utterance cuts on natural speech pauses.
- Per-session conversation history added to `SessionContext` — LLM no longer treats every utterance as a fresh call. `generate_stream_with_messages` added to `LLMEngine` to accept pre-built message history.

**Milestone 5 — AetherRTC: outbound audio path** ✅

- `EncodeUlaw` implemented in `pkg/codec/g117.go` — PCM16 LE → 8-bit µ-law.
- `TrackLocalStaticSample` (PCMU, 8kHz) added to `PeerSession` before `ProcessOffer`.
- `rtpSender.ReadRTCP()` drain goroutine added — prevents Pion sender buffer from backing up.
- Outbound goroutine: accumulates PCM into buffer, drains in exact 320-byte (20ms) frames, ticker-paced at 20ms intervals, `EncodeUlaw` → `WriteSample`.
- `AgentSpeaking` atomic bool gates inbound microphone capture while AI is speaking — prevents acoustic feedback loop.
- `AgentSpeaking` clears unconditionally on 300ms idle timeout (not conditional on `len(pcmBuffer) == 0` — that condition is permanently false due to sub-frame PCM remainders after the last TTS sentence).
- `PCMOutboundChan` send in `stream_manager.go` is blocking with `DoneChan` escape, not `default:` drop — ensures audio reaches the playback goroutine under normal operation.
- Verified: response audio audible in browser tab.

**Known limitation identified during Milestone 5:**
First-run VAD detection lag (~5–10 seconds from process start) appears to be Piper ONNX graph initialization cost on first `synthesize()` call combined with Ollama model load time — not a VAD bug. Second run within the same process is immediate. Not yet confirmed with timestamps; revisit if it persists or worsens.

---

## Active Backlog

### 1. AetherRTC Integration — Milestone 6: End-to-end verification

**Priority:** High — immediate next milestone.

- [ ] Full lifecycle test: connect, speak multiple utterances, disconnect —
      verify `Session.Terminate()` and `PeerSession.Close()` both fire
      cleanly with no goroutine leaks on either side.
- [ ] Confirm a single `session_id` is identical across AetherRTC,
      Orchestrator-Go, and Python logs for one call.
- [ ] Confirm no goroutine leak on browser disconnect — specifically that
      `bridge.RunSession` exits cleanly and `DoneChan` closes on both sides.

Exit criteria: three full turns of conversation, clean disconnect, all three
logs show the same session ID, no stale goroutines.

### 2. Monitor Goroutine (Go)

**Priority:** Medium. Deferred until Milestone 6 is verified.

A third goroutine inside `Session.Run()` watching for:

- Context cancellation (caller disconnects).
- Session timeout (inference stalls beyond a threshold).
- Signal on `InterruptChan` (barge-in: user speaks while AI is speaking).

On any of the above, the monitor must flush `AgentAudioChan`, cancel
the active `readPump` receive, and transition the session back to
`ACTIVE` to accept new audio.

### 3. Concurrent Ordered Utterance Processing

**Priority:** Low. Future enhancement, not current scope.

Becomes relevant when barge-in is in scope and utterance overlap is the
normal path rather than an edge case. Requires sequence-numbered utterances
and an ordering queue on the outbound side.

### Phase 2: Extensibility and Tool Calling (deferred, tracked only)

**Priority:** Not scoped yet. Explicitly out of Phase 1.

Tool calling, RAG, MCP integration, and broader agent extensibility deferred
until Phase 1 (single-user, VAD-driven, AetherRTC-connected pipeline) is
stable and Milestone 6 verified. This phase touches only `inference-python`
— no bearing on AetherRTC or the gateway contract.

---

## Reference Documents

- [`docs/HLD.md`](HLD.md) — High Level Design
- [`docs/LLD.md`](LLD.md) — Low Level Design
- [`docs/true_duplex.md`](true_duplex.md) — True Duplex implementation
  design, milestone breakdown, and architectural decisions
- [`docs/vad.md`](vad.md) — VAD integration design, milestone breakdown,
  and architectural decisions
- [`docs/three-tier-architecture.md`](three-tier-architecture.md) —
  AetherRTC / Orchestrator-Go / Inference-Python topology, proto
  contracts, and full session lifecycle walkthrough
