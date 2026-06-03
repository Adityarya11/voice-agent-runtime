# Low Level Design (LLD)

# Project

Voice Runtime

A distributed, streaming-first voice agent runtime built using Go and Python.

---

# Goals

The runtime must support:

- Real-time audio streaming
- Bidirectional communication
- Concurrent session handling
- Pluggable AI models
- Future telephony integration
- Future memory integration
- Future tool invocation
- Domain-independent execution

---

# Service Topology

```text
Client Audio
     │
     ▼

┌──────────────────────┐
│   GO ORCHESTRATOR    │
└──────────────────────┘

     │ gRPC Stream

     ▼

┌──────────────────────┐
│ PYTHON INFERENCE     │
└──────────────────────┘

     │
     ▼

Audio Response
```

---

# Core Design Principle

The system is divided into:

1. Control Plane
2. Data Plane

---

## Control Plane

Responsible for:

- Session lifecycle
- Configuration
- Call state
- Routing
- Resource ownership

Implemented in Go.

---

## Data Plane

Responsible for:

- Audio transport
- STT
- LLM
- TTS

Implemented across Go and Python.

---

# Session Model

Every conversation is represented by a Session.

```go
type Session struct {

    SessionID string

    State SessionState

    StartedAt time.Time

    UserAudioChan chan []byte

    AgentAudioChan chan []byte

    InterruptChan chan bool

    DoneChan chan bool
}
```

---

# Session Lifecycle

State Machine

```text
CREATED

↓

CONNECTING

↓

ACTIVE

↓

PROCESSING

↓

RESPONDING

↓

ACTIVE

↓

TERMINATED
```

---

## CREATED

Session object allocated.

No active streams.

---

## CONNECTING

gRPC stream established.

Resources allocated.

Channels initialized.

---

## ACTIVE

Waiting for incoming audio.

No inference running.

---

## PROCESSING

Audio currently undergoing:

- VAD
- STT
- LLM

---

## RESPONDING

TTS stream being returned.

---

## TERMINATED

Cleanup phase.

Resources released.

Session removed.

---

# Go Orchestrator Design

The orchestrator owns:

- Session lifecycle
- Audio routing
- Future telephony routing

The orchestrator never performs inference.

---

# Goroutine Model

Every session receives independent goroutines.

```text
Session

├── Audio Receive Loop

├── Audio Send Loop

├── gRPC Stream Loop

└── Lifecycle Loop
```

This prevents one session from blocking another.

---

# Audio Receive Loop

Responsibility:

Receive audio from source.

Future source examples:

- Microphone
- SIP
- Twilio
- WebRTC

Pseudo Flow

```text
Audio Source

↓

Receive Bytes

↓

Push To UserAudioChan
```

---

# gRPC Stream Loop

Reads from:

```go
UserAudioChan
```

Writes to:

Python gRPC Stream

```text
UserAudioChan

↓

gRPC Client

↓

Python Service
```

---

# Audio Send Loop

Reads:

```go
AgentAudioChan
```

Writes:

Audio Sink

Future examples:

- Speaker
- Phone Call
- SIP Endpoint

---

# Future Interrupt Loop

Monitors:

```go
InterruptChan
```

Purpose:

Support barge-in.

Example:

User begins speaking while AI is speaking.

System should:

```text
Cancel TTS

Flush Audio Buffer

Resume Listening
```

---

# Python Inference Engine

Responsible for:

- Audio understanding
- Reasoning
- Response generation

The engine is implemented as a pipeline.

---

# Inference Pipeline

```text
Audio

↓

VAD

↓

STT

↓

Prompt Builder

↓

LLM

↓

TTS

↓

Audio
```

Every stage should remain independently replaceable.

---

# Stage 1

Voice Activity Detection

Module:

```text
vad/
```

Candidate:

```text
Silero VAD
```

Purpose:

Detect speech boundaries.

Prevent unnecessary STT execution.

---

# Stage 2

Speech To Text

Module:

```text
stt/
```

Candidate:

```text
Faster Whisper Small
```

Purpose:

Convert speech to text.

Input:

```text
Audio Chunks
```

Output:

```text
Transcript
```

---

# Stage 3

Prompt Builder

Module:

```text
app/
```

Responsibility:

Construct final prompt.

Current Input:

```text
Transcript
```

Future Input:

```text
Transcript

Memory Context

Tool Results

Agent Profile
```

Output:

```text
Final Prompt
```

---

# Stage 4

LLM Engine

Module:

```text
llm/
```

Candidate:

```text
Qwen 2.5 3B

Phi 3 Mini
```

Responsibilities:

- Reasoning
- Tool Selection
- Response Generation

Output:

```text
Response Text
```

Future:

Streaming token generation.

---

# Stage 5

Text To Speech

Module:

```text
tts/
```

Candidate:

```text
Piper
```

Input:

```text
Response Text
```

Output:

```text
Audio Bytes
```

Returned through gRPC stream.

---

# Agent Profile Design

Agent behavior must never be hardcoded.

Profile Example

```yaml
name: receptionist

prompt: |
  You are a receptionist.

tools:
  - calendar
```

Another Profile

```yaml
name: realtor

prompt: |
  You are a real estate consultant.

tools:
  - property_search
```

The runtime remains unchanged.

Only profile changes.

---

# gRPC Contract Design

Version 1

```protobuf
service VoiceAgent {

    rpc StreamAudio(
        stream AudioChunk
    )
    returns (
        stream AudioChunk
    );
}
```

Future versions should evolve toward event-based messaging.

Example:

```protobuf
message Event {

    string session_id = 1;

    oneof payload {

        AudioChunk audio = 2;

        Transcript transcript = 3;

        Token token = 4;

        ToolResult tool = 5;

        ControlSignal control = 6;
    }
}
```

This avoids future protocol redesign.

---

# Future Memory Layer

Version 1:

No memory.

Version 2:

Hot Memory

```text
StrataKV
```

Responsibilities:

- Active session state
- Recent context

Cold Memory

```text
PostgreSQL
```

Responsibilities:

- Historical conversations
- Analytics
- Long-term storage

---

# Future Tool Execution Layer

Architecture

```text
LLM

↓

Tool Decision

↓

Tool Registry

↓

Tool Execution

↓

Tool Result

↓

Prompt Update
```

Tool Registry Examples

```text
Calendar

CRM

Email

Search

Custom APIs
```

---

# Failure Handling

Every subsystem should fail independently.

Example:

TTS Failure

```text
Fallback Audio

Session Continues
```

LLM Failure

```text
Return Safe Response

Keep Session Alive
```

gRPC Failure

```text
Terminate Session

Release Resources
```

No subsystem should crash the runtime.

---

# Concurrency Strategy

One Session

=

One Resource Boundary

No shared mutable state between sessions.

Future shared state:

```text
Metrics

Memory

Configuration
```

must be protected through:

```go
sync.Mutex

sync.RWMutex

atomic
```

where appropriate.

---

# Observability Roadmap

Future Metrics

```text
Active Sessions

Average Latency

STT Latency

LLM Latency

TTS Latency

Session Duration
```

Future Logging

```text
Session Created

Session Terminated

Inference Started

Inference Completed

Tool Invoked
```

---

# Version 1 Definition Of Done

The runtime is considered operational when:

1. Go establishes a gRPC stream.

2. Audio bytes are transmitted.

3. Python receives audio.

4. Python returns audio.

5. Go receives audio.

6. Multiple concurrent sessions execute independently.

No telephony, memory, tools, or databases are required for Version 1 completion.

Those capabilities are future evolutionary layers built on top of a stable runtime foundation.
