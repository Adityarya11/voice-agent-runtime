# Session Management — Work Log & Backlog

## What was done in this iteration

### Dynamic Profiling (previous iteration)

- `StreamEvents` in `main.py` now reads the incoming `ControlSignal` before
  processing audio chunks.
- The `system_prompt` from the agent profile YAML is extracted and passed
  directly into `LLMEngine.generate()` via the existing `system_override`
  parameter.
- The fallback system prompt in `engine.py` is now a named module-level
  constant instead of an anonymous inline string.
- Verified: LLM responds as the loaded agent persona, not as a generic
  assistant.

### Session Management (this iteration)

- `session.go` rewritten from a passive data struct into an active controller.
- `Session` now owns the gRPC stream reference directly via `Attach()`.
- State machine is enforced. Transitions are validated against a legal
  transition map. Illegal transitions log an error and no-op instead of
  panicking or silently corrupting state.
- `writePump()` goroutine owns all outbound gRPC sends. Drains
  `UserAudioChan`, calls `CloseSend()` on exhaustion, transitions session
  to `PROCESSING`.
- `readPump()` goroutine owns all inbound gRPC receives. Transitions session
  to `RESPONDING` on first audio chunk received. Drains response into
  `AgentAudioChan`. Closes `AgentAudioChan` and `DoneChan` on EOF.
- `main.go` is now decoupled from gRPC entirely. It feeds `UserAudioChan`
  and reads `AgentAudioChan`. It has no knowledge of the stream internals.
- `InterruptChan` is allocated and reserved for barge-in support.

### Token Streaming from LLM (done)

- Introduced `generate_stream()` with Ollama streaming (`stream=1`) and persistent model residency (`keep_alive=-1`) to eliminate cold-start overhead and enable incremental response delivery.
- Pinned STT language to English (`language="en"`), bypassing auto-detection and reducing transcription latency from **`~0.9s to ~0.7s`** in test runs.
- Achieved first-token latency (TTFT) of ~0.48s, with initial TTS audio streamed in ~0.06s and end-to-end `STT → LLM → TTS` response completion in **`~1.1–1.2s`** under favorable conditions.

### True Duplex — Milestone 2: Single Utterance Duplex Path (completed) [PULL#6](https://github.com/Adityarya11/voice-agent-runtime/pull/6)

- `END_OF_UTTERANCE` control signal added to proto as `SignalType = 3`.
  Proto regenerated on both Go and Python sides.
- `main.py` rewritten with three-worker architecture: `_read_pump` thread
  owns inbound stream exclusively, per-utterance `_run_utterance` daemon
  threads dispatch inference without blocking the listener, main thread
  drains outbound queue and yields to gRPC.
- `session.go` state machine updated: `ACTIVE → RESPONDING` direct
  transition added, `writePump` sends `END_OF_UTTERANCE` instead of
  calling `CloseSend()`, stream stays open after utterance boundary.
- `context.is_active()` guard added to `_run_utterance` to abort
  in-flight inference cleanly when the gRPC context is cancelled.
- Verified: Go stream stays open after sending `END_OF_UTTERANCE`.
  Python detects boundary, dispatches inference, streams response back.
  Both graceful and ungraceful shutdown scenarios handled correctly on
  both sides.

---

## Backlog

### 1. Monitor Goroutine

**Priority:** High before telephony integration.

A third goroutine inside `Session.Run()` that watches for:

- Context cancellation (caller hangs up).
- Timeout (inference takes too long, session stalls).
- A signal on `InterruptChan` (user speaks while agent is speaking — barge-in).

On interrupt, the monitor must:

- Cancel the current `readPump` receive by closing the stream context.
- Flush `AgentAudioChan` so no stale audio is played.
- Transition the session back to `ACTIVE` so it can accept new user audio.

This goroutine is what makes the system feel like a real phone call instead
of a walkie-talkie.

### 2. DoneChan ownership clarification

**Priority:** Medium.

`DoneChan` is currently closed by `readPump` on EOF. This is correct for
the happy path. When the monitor goroutine is introduced, there will be
multiple goroutines that could legitimately signal session completion
(EOF, timeout, context cancel, fatal error). Closing a channel twice panics.

The fix is a `sync.Once` wrapping the `close(s.DoneChan)` call so that
whichever goroutine reaches it first wins and subsequent calls are no-ops.

```go
var closeOnce sync.Once
closeOnce.Do(func() {
    close(s.DoneChan)
})
```

This should be added when the monitor goroutine is implemented.

<!-- ### 3. Token streaming from LLM

**Priority:** High. This is the next major milestone.

Currently `LLMEngine.generate()` sets `stream=False` in the Ollama call.
The full response is buffered before TTS begins. This means the user waits
for the entire LLM response before hearing anything.

The correct pipeline is:

- `ollama.chat()` with `stream=True` returns a token iterator.
- Tokens are accumulated into sentence-boundary chunks
  (period, question mark, exclamation mark detection).
- Each sentence chunk is passed to TTS immediately.
- TTS audio for sentence one is already streaming back to Go while the LLM
  is still generating sentence two.

This collapses the STT → LLM → TTS latency stack from sequential to
overlapping and is the single largest perceived latency improvement
available. -->

### 3. True bidirectional duplex

**Priority:** High. Depends on token streaming being done first.

Currently the stream is half-duplex. Go sends all audio, closes send,
then waits. Python buffers all audio, processes, then sends all audio back.

True duplex means:

- Go can send audio while simultaneously receiving audio.
- Python can begin responding before all user audio has arrived
  (VAD-gated: once silence is detected, inference begins immediately
  without waiting for `CloseSend()`).

This requires VAD integration on the Python side and the monitor goroutine
on the Go side to be in place first.

### 4. VAD integration

**Priority:** Depends on duplex work.

Faster-Whisper has a built-in `vad_filter=True` flag which is already set
in `transcriber.py`. However this operates on a complete audio file after
the fact. True VAD means detecting speech boundaries in the live stream
so inference can begin mid-call without waiting for the stream to close.

Candidate: Silero VAD running on CPU alongside Faster-Whisper.
