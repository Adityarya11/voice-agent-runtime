# Voice Agent Runtime

A distributed, self-hosted voice agent runtime built across a Go orchestrator
and a Python inference engine, connected over a custom gRPC bidirectional
stream. The runtime is designed so that agent behavior — persona, system
prompt, domain — is entirely driven by configuration, not by code.

The same inference pipeline that powers a dental clinic receptionist can
power a real estate consultant or an admissions counselor. Only the YAML
profile changes.

> **Status: Active development.** The gRPC bridge, local inference pipeline,
> dynamic agent profiling, session state management, and token-streaming
> response generation are functional. The runtime currently operates in
> half-duplex mode. True bidirectional duplex, VAD-gated speech detection,
> and a barge-in monitor are in progress. Public MVP and latency benchmarks
> targeted for **[07/2026]**.

---

## Why this exists

Self-hosting a voice AI pipeline today usually means either paying for
managed cloud APIs end to end, or stitching together STT, LLM, and TTS tools
with no coherent transport layer, no session model, and no clean separation
between orchestration and inference. This project is an attempt to build
that missing layer — a runtime, not a single-purpose app — that runs
entirely on local hardware with no external API dependencies.

Full architectural reasoning, including why Go for orchestration, why
Python for inference, why gRPC over REST, and the complete session state
machine design, is documented in:

- [`docs/HLD.md`](docs/HLD.md) — High Level Design
- [`docs/LLD.md`](docs/LLD.md) — Low Level Design

---

## Architecture

```text
┌─────────────────────────────────────┐
│           GO ORCHESTRATOR           │
│                                     │
│  Session lifecycle, state machine,  │
│  channel-based audio routing,       │
│  writePump / readPump goroutines    │
│                                     │
└──────────────┬──────────────────────┘
               │ gRPC bidirectional stream
               │ unified Event proto (oneof: audio | transcript | control)
               ▼
┌─────────────────────────────────────┐
│       PYTHON INFERENCE ENGINE       │
│                                     │
│  Faster-Whisper (STT, CPU)          │
│  Ollama qwen2.5:3b (LLM, GPU)       │
│  Piper TTS (TTS, CPU)               │
│  Dynamic agent profile injection    │
│                                     │
└─────────────────────────────────────┘
```

The orchestrator never touches AI models. The inference engine never
manages call lifecycle. Each is replaceable independently of the other.

---

## Companion Project: AetherRTC (Edge Media Gateway)

Currently, the orchestrator routes local `.wav` files for testing. To bring this runtime to the public web without polluting the orchestration logic with complex network protocols, a companion project is actively being developed: **[AetherRTC](https://github.com/Adityarya11/atherRTC)**.

AetherRTC is a standalone WebRTC Edge Media Gateway built in Go (using Pion). It acts as the "front door" for the Voice Agent Runtime by:

- Terminating public WebRTC connections (SDP negotiation, ICE traversal, DTLS handshakes).
- Handling all media codecs at the edge (forcing G.711/PCMU to avoid heavy Opus CGO decoding).
- Streaming clean, raw PCM audio bytes directly into the Voice Orchestrator via an internal gRPC bridge.

This 3-tier service mesh design ensures the Voice Agent Runtime remains strictly focused on AI orchestration and session state, entirely ignorant of how the audio arrived.

---

## Hardware-aware by design

This runtime was built and tested entirely on a consumer laptop — RTX 3050
Mobile (4GB VRAM), Ryzen 5 6600H, 16GB DDR5 — and every component placement
decision reflects that constraint rather than ignoring it.

| Stage | Component             | Device | Footprint       |
| ----- | --------------------- | ------ | --------------- |
| STT   | Faster-Whisper (int8) | CPU    | negligible VRAM |
| LLM   | qwen2.5:3b via Ollama | GPU    | ~2.2GB VRAM     |
| TTS   | Piper                 | CPU    | negligible VRAM |

Nothing competes for the GPU. Nothing OOMs. The split is deliberate, not
accidental.

---

## What's working today

- [x] gRPC bidirectional stream between Go orchestrator and Python inference engine
- [x] Full STT → LLM → TTS inference pipeline running locally, no external API calls
- [x] Dynamic agent profiling — system prompts flow from YAML configs through the proto contract into the LLM at session start
- [x] Session state machine in Go with mutex-protected, validated transitions (`CREATED → CONNECTING → ACTIVE → PROCESSING → RESPONDING → TERMINATED`)
- [x] Decoupled I/O — `writePump` and `readPump` goroutines own all gRPC traffic, isolating orchestration logic from transport internals
- [x] Token-streaming LLM responses with sentence-boundary chunking — TTS begins synthesizing the first sentence while the LLM is still generating later sentences

## What's in progress

- [ ] True bidirectional duplex (currently half-duplex — Go sends all audio, closes send, then waits)
- [ ] VAD-gated speech boundary detection so inference can begin before the caller finishes speaking
- [ ] Monitor goroutine for barge-in handling, timeout detection, and context cancellation
- [ ] `sync.Once` guard on session completion signaling to safely support multiple completion sources
- [ ] Per-stage latency instrumentation (STT, LLM time-to-first-token, TTS, end-to-end) and a recorded demo
- [ ] Removal of the temp-file boundary in Python STT in favor of true streaming transcription

---

## Project structure

```text
voice-runtime/
├── proto/
│   └── agent.proto              # Unified Event contract (audio | transcript | control)
├── services/
│   ├── orchestrator-go/         # Control plane — session lifecycle, gRPC client
│   └── inference-py/            # Data plane — STT, LLM, TTS pipeline
├── configs/
│   └── agent_profiles/          # YAML-defined agent personas
├── docs/
│   ├── HLD.md
│   ├── LLD.md
│   └── backlog.md               # Detailed engineering log and architectural debt tracker
└── test_data/                   # Sample input/output audio for local testing
```

Service-specific setup and run instructions:

- [`services/orchestrator-go/README.md`](services/orchestrator-go/README.md)
- [`services/inference-py/README.md`](services/inference-py/README.md)

---

## Engineering log

Detailed notes on what was built in each iteration, design tradeoffs made,
and known architectural debt being tracked are kept in
[`docs/backlog.md`](docs/backlog.md). This is the most accurate source of
truth for the current state of the system beyond what's summarized here.
