# Three-Tier Architecture — AetherRTC / Orchestrator-Go / Inference-Python

## Status

Design record. Written to resolve architectural ambiguity before further
implementation. Supersedes any prior assumption that AetherRTC connects
directly to the Python inference engine. This document is the source of
truth for how the three services relate; `true_duplex.md` and `vad.md`
remain the source of truth for what happens _inside_ the Python engine.

---

## 1. The Topology

```
Browser <--WebRTC--> AetherRTC <--gRPC--> Orchestrator-Go <--gRPC--> Inference-Python
         (Pion)                (gateway.proto)          (agent.proto)
```

Three independently deployable services. Each hop is a distinct gRPC
relationship with a distinct contract. No service skips a tier. AetherRTC
never talks to Python directly, and never will — that would let two
different callers hit Python's session model, which is exactly the kind
of split-brain risk the state machine in `session.go` was built to
prevent in the first place.

---

## 2. Resolving the Client/Server Confusion

"gRPC server" and "gRPC client" are wire roles — which process opens the
listening port versus which process dials in. They say nothing about
which service is in charge of anything. Do not infer authority from this
label.

| Relationship                       | Who is the gRPC server | Who is the gRPC client | Who owns session lifecycle authority |
| ---------------------------------- | ---------------------- | ---------------------- | ------------------------------------ |
| AetherRTC ↔ Orchestrator-Go        | **Orchestrator-Go**    | AetherRTC              | Orchestrator-Go                      |
| Orchestrator-Go ↔ Inference-Python | **Inference-Python**   | Orchestrator-Go        | Orchestrator-Go                      |

Orchestrator-Go is a **gRPC server to AetherRTC** and a **gRPC client to
Python**, simultaneously. This is an entirely ordinary shape for a
middle-tier service — it is not a special case, and it is not in tension
with anything already built. Orchestrator-Go remains the single
authority over session state (`CREATED → ... → TERMINATED`) regardless
of which direction the TCP connection was dialed. Inference-Python and
AetherRTC are both, from Go's point of view, dependencies it calls or is
called by — neither is "in charge."

---

## 3. Two Contracts, Deliberately Different Scopes

### 3.1 `agent.proto` — Orchestrator-Go ↔ Inference-Python (existing, unchanged)

This contract is AI-aware. It carries `AgentProfile`, `Transcript`,
`ControlSignal` types for `BARGE_IN` and `END_OF_UTTERANCE`. It exists
because Go needs to hand Python enough context to run inference and
Python needs to hand Go enough signal to manage the session. Nothing in
this document changes it. VAD, STT, LLM, and TTS all remain entirely
inside Python, invisible to both Go and AetherRTC.

### 3.2 `gateway.proto` — AetherRTC ↔ Orchestrator-Go (new, to be built)

This contract must **not** be a copy of `agent.proto`. It must not carry
`AgentProfile`, transcripts, or any AI-layer concept. AetherRTC's one
rule stands: it does not know what an utterance is, does not know what
Silero VAD is, does not know what a system prompt is. Its only currency
is raw audio bytes and connection-level metadata.

```protobuf
syntax = "proto3";
package gateway;

service Gateway {
  rpc StreamAudio(stream GatewayEvent) returns (stream GatewayEvent);
}

message GatewayEvent {
  string session_id = 1;
  oneof payload {
    AudioChunk audio = 2;
    GatewayControl control = 3;
  }
}

message AudioChunk {
  bytes data = 1;
}

message GatewayControl {
  enum SignalType {
    START_SESSION = 0;
    END_SESSION = 1;
  }
  SignalType type = 1;
  int32 source_sample_rate = 2;
}
```

This mirrors `agent.proto`'s `oneof Event` shape deliberately — same
pattern, smaller scope. `GatewayControl` exists only to carry
connection-level metadata (start, end, sample rate), never AI-layer
metadata. Sample rate negotiation happens here, not by AetherRTC doing
any resampling itself.

**Where does `AgentProfile` come from, if not from AetherRTC?**
Orchestrator-Go owns it, loaded the same way it does today — via
`-profile` flag or per-deployment config in `configs/agent_profiles/`.
A hosted deployment serving one client's dental receptionist runs
Orchestrator-Go configured with that profile; AetherRTC never carries
persona information, because persona is not a transport concern.

---

## 4. Resolving the `??` in Orchestrator-Go

The diagram's open question — what does Orchestrator-Go actually do
now — resolves to two concrete new responsibilities, neither of which
existed before AetherRTC entered the picture:

### 4.1 A new gRPC server component

Orchestrator-Go must run a second gRPC server (distinct port from
Python's `50051` — e.g. `50052`) implementing the `Gateway` service.
This is new code, living alongside the existing `readPump`/`writePump`
machinery, not replacing it.

### 4.2 A session bridge between the two protocols

For each incoming `gateway.proto` connection, Orchestrator-Go:

1. Receives `GatewayControl{START_SESSION, source_sample_rate}` from
   AetherRTC.
2. Creates a `Session` exactly as it does today (`NewSession`, `Attach`
   to a _new_ `agent.proto` stream to Python).
3. Translates the sample rate into an `agent.proto` `ControlSignal`
   with `START_SESSION` — this requires adding `source_sample_rate`
   as a new field on `agent.proto`'s existing `ControlSignal` message
   (tag `= 3`; purely additive, does not affect Go's own current test
   harness or any already-compiled consumer).
4. Relays inbound `AudioChunk` bytes from the AetherRTC stream directly
   into the Python stream — this replaces `main.go`'s current pattern
   of reading from a `.wav` file and calling `StreamAudio`/
   `StreamUtterance`. The _source_ of audio changes from file to
   network; the mechanism downstream is unchanged.
5. Relays outbound audio the reverse direction: `readPump` already
   receives synthesized audio from Python onto `AgentAudioChan` today
   — this is proven, working code. Instead of `main.go` writing those
   bytes to `test_data/output_m3.raw`, a new relay writes them onto the
   `gateway.proto` stream back to AetherRTC.

**One `session_id` flows through all three tiers unmodified.** AetherRTC
generates it (it owns the `PeerSession`, and thus the first point of
contact with the browser), passes it in `GatewayEvent.session_id`, and
Orchestrator-Go reuses the identical string as the `agent.proto`
`Event.session_id` sent to Python. This gives you single-string
traceability through logs across all three services.

### 4.3 What does _not_ need to change

- Python's `main.py`, `VoiceAgentServicer`, VAD, STT, LLM, TTS — all
  untouched. Python does not know or care that a second gRPC client
  exists on the other end of Go.
- The session state machine (`CREATED → ... → TERMINATED`) and its
  legal-transition map — unchanged. It gains a new _trigger_ (an
  inbound `gateway.proto` connection instead of a manually-driven test
  harness call) but no new states.
- `agent.proto`'s existing fields — unchanged, only additive.

---

## 5. Full Session Lifecycle Walkthrough

```
1. Browser opens WebSocket, sends SDP offer.
   AetherRTC: signaling/server.go creates PeerSession(sessionID).

2. WebRTC negotiation completes. Browser begins sending G.711 audio.
   AetherRTC: OnTrack decodes to PCM, pushes to PCMInboundChan.

3. AetherRTC's bridge (internal/bridge/) opens a gateway.proto
   StreamAudio call to Orchestrator-Go:50052, sends
   GatewayControl{START_SESSION, source_sample_rate: 8000}.

4. Orchestrator-Go's new Gateway server implementation:
   - creates Session(sessionID)
   - opens agent.proto StreamEvents to Python:50051
   - sends ControlSignal{START_SESSION, source_sample_rate: 8000,
     profile: <loaded from local config>}

5. AetherRTC drains PCMInboundChan, wraps each chunk in
   GatewayEvent{AudioChunk}, sends over the gateway.proto stream.

6. Orchestrator-Go's bridge relays each AudioChunk into the
   agent.proto stream to Python, unmodified bytes.

7. Python's AudioPreprocessor resamples 8kHz -> 16kHz per the
   negotiated source_sample_rate, feeds Silero VAD, detects utterance
   boundaries autonomously (per vad.md — no explicit signal needed
   from Go or AetherRTC).

8. Python runs STT -> LLM -> TTS, streams synthesized audio back over
   agent.proto as AudioChunk events.

9. Orchestrator-Go's readPump receives these (existing, proven code),
   pushes to AgentAudioChan.

10. A new relay goroutine drains AgentAudioChan, wraps bytes as
    GatewayEvent{AudioChunk}, sends back over the gateway.proto
    stream to AetherRTC.

11. AetherRTC encodes PCM -> G.711, writes to the outbound RTP track,
    browser plays the response.
```

---

## 6. What Remains Explicitly Out of Scope

Per prior discussion — this milestone proves one browser call working
end to end. The following are named and deferred, not forgotten:

- Multi-tenancy, auth, API keys, billing for AetherRTC as a hosted
  product.
- TURN server configuration for real-world NAT traversal (STUN-only
  today; acceptable for local/same-network testing, not for public
  internet users behind symmetric NATs).
- Horizontal scaling of either service.
- Barge-in (depends on the still-pending monitor goroutine in
  `session.go`).
- Concurrent ordered utterance processing.

## 7. Phase 2 — Deferred, Tracked Only

Tool calling, RAG, MCP integration, and any broader agent extensibility
remain explicitly out of scope until the three-tier pipeline above is
proven end to end and stable. This phase touches only
`inference-python` — it has no bearing on AetherRTC or the gateway
contract, since tools/RAG are a reasoning-layer concern that lives
entirely behind Python's existing boundary with Go. Noted here so intent
isn't lost, not to be expanded until Phase 1 (this document) is complete.

---

## 8. Immediate Next Steps

1. Finalize `gateway.proto` as drafted in §3.2 — confirm field names
   and tag numbers before generating code in either repo.
2. Add `source_sample_rate` to `agent.proto`'s `ControlSignal` (tag
   `= 3`), regenerate Go and Python bindings **in VAR's repo only**.
3. Generate `gateway.proto` bindings **in AetherRTC's repo only**
   (separate Go module, separate generated code — no shared bindings
   between the two repos at this stage).
4. Build Orchestrator-Go's new `Gateway` server implementation and the
   session bridge described in §4.2.
5. Build AetherRTC's `internal/bridge/` — `client.go` (dials
   Orchestrator-Go, sends `START_SESSION`) and `stream_manager.go`
   (drains `PCMInboundChan`, sends `AudioChunk`s, receives the reverse
   stream).
6. Verify against a single real browser tab before touching anything
   in §6.
