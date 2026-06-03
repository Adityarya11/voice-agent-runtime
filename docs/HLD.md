# High Level Design (HLD)

## Project

Voice Runtime

A distributed, real-time voice agent runtime designed to power multiple domain-specific AI calling agents through configurable prompts, tools, and memory layers.

The system is designed as a platform rather than a single-purpose application.

Examples include:

- Real Estate Caller
- Restaurant Receptionist
- College Admission Counselor
- Customer Support Agent
- Appointment Scheduler

without requiring architectural changes to the runtime itself.

---

# Vision

The runtime should provide a generic infrastructure capable of:

- Real-time bidirectional audio streaming
- Stateful conversations
- Tool invocation
- Long-term memory integration
- Concurrent call orchestration
- Agent profile configuration
- Local model execution

The goal is to separate infrastructure from business logic.

The runtime should remain identical regardless of the use case.

---

# Architectural Principles

## 1. Runtime First

The system is designed as a runtime platform.

Domain-specific behavior must never be embedded into infrastructure components.

Bad:

```text
RealEstateService
PropertyLookupModule
```

Good:

```text
AgentProfile
ToolRegistry
KnowledgeProvider
```

---

## 2. Local First

All inference components should be capable of running locally.

This reduces:

- API costs
- Vendor lock-in
- External dependencies

Initial target models:

STT:

- Faster Whisper Small

LLM:

- Qwen 2.5 3B
- Phi-3 Mini

TTS:

- Piper TTS

---

## 3. Streaming First

The system should be built around streaming rather than request-response workflows.

Every subsystem must support streaming.

Examples:

- Audio streaming
- Token streaming
- TTS chunk streaming

Streaming reduces perceived latency and improves responsiveness.

---

## 4. Concurrent By Design

The system must support multiple simultaneous conversations.

Concurrency is a core architectural requirement.

Go is selected as the orchestration layer because of:

- Goroutines
- Channels
- Lightweight scheduling
- Efficient networking

---

## 5. Modular Evolution

Every subsystem should be replaceable.

Examples:

STT

```text
Faster Whisper
↓
Whisper.cpp
↓
Future Model
```

LLM

```text
Qwen
↓
Llama
↓
Future Model
```

TTS

```text
Piper
↓
XTTS
↓
Future Model
```

The runtime should not depend on specific model implementations.

---

# System Architecture

The system is composed of two primary services.

```text
+------------------------------------------------+
|            GO ORCHESTRATOR                     |
+------------------------------------------------+
|                                                |
| Session Management                             |
| Audio Routing                                  |
| Call Lifecycle                                 |
| Concurrency Control                            |
| gRPC Client                                    |
| Future Telephony Integration                   |
| Future Memory Coordination                     |
|                                                |
+-------------------+----------------------------+
                    |
                    |
                    | gRPC Streaming
                    |
                    v
+------------------------------------------------+
|            PYTHON INFERENCE ENGINE             |
+------------------------------------------------+
|                                                |
| Voice Activity Detection                       |
| Speech To Text                                 |
| Prompt Construction                            |
| LLM Inference                                  |
| Tool Execution                                 |
| Text To Speech                                 |
|                                                |
+------------------------------------------------+
```

---

# Core Responsibilities

## Go Orchestrator

The orchestrator is responsible for system execution and lifecycle management.

Responsibilities:

- Session creation
- Session termination
- Audio transport
- Concurrent call handling
- Streaming management
- Future telephony integration
- Future memory integration

The orchestrator must not perform AI inference.

---

## Python Inference Engine

The inference engine is responsible for all AI-related operations.

Responsibilities:

- Voice Activity Detection
- Speech Recognition
- Prompt Building
- Tool Invocation
- LLM Generation
- Speech Synthesis

The inference engine must not manage call lifecycles.

---

# Agent Profile System

The runtime should support multiple agent personalities through configuration.

Example:

```yaml
agent:
  name: Real Estate Agent

prompt: |
  You are a professional property consultant.

tools:
  - property_search
  - visit_scheduler
```

Another profile:

```yaml
agent:
  name: Restaurant Receptionist

prompt: |
  You are a friendly restaurant receptionist.

tools:
  - reservation_tool
  - menu_lookup
```

The runtime remains unchanged.

Only configuration changes.

---

# Future Memory Architecture

Memory is intentionally excluded from Version 1.

Future design:

L1 Hot Memory

```text
StrataKV
```

Purpose:

- Active session context
- Fast retrieval

L2 Cold Memory

```text
PostgreSQL
```

Purpose:

- Historical conversations
- Analytics
- Long-term storage

---

# Future Tool Architecture

The inference engine will expose a generic Tool Registry.

Example:

```text
Calendar Tool
CRM Tool
Email Tool
Knowledge Search Tool
```

Agent profiles determine which tools are available.

The runtime itself remains tool-agnostic.

---

# Future Telephony Layer

Version 1 uses local audio simulation.

Future versions may support:

- SIP
- WebRTC
- Twilio
- VAPI
- Custom PBX Systems

The orchestrator should expose a transport abstraction layer so telephony providers can be swapped without affecting inference logic.

---

# Success Criteria

Version 1 is considered successful when:

1. Go streams audio to Python.

2. Python processes the audio.

3. Python returns synthesized audio.

4. Go plays the response.

5. Multiple concurrent sessions can execute independently.

No memory, database, telephony, or external tools are required for Version 1 success.

Those capabilities are planned as future architectural extensions.
