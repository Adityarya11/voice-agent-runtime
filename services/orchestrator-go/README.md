# Orchestrator — Go

The orchestrator is the control plane of the voice runtime. It manages
session lifecycle, owns the gRPC stream to the inference engine, and routes
audio through isolated per-session goroutines. It has no knowledge of AI
models or inference internals.

---

## Responsibilities

- Load agent profile from YAML configuration
- Create and manage session state machine
- Stream user audio to the inference engine
- Receive synthesized audio back and write to output
- Enforce clean session lifecycle from CREATED through TERMINATED

---

## Requirements

- Go 1.21 or later
- Inference engine running on `localhost:50051`
- Input audio file at `test_data/input.wav` (relative to repo root)
- Agent profile YAML present in `configs/agent_profiles/`

---

## Setup

```bash
cd services/orchestrator-go
go mod download
```

---

## Running

```bash
cd services/orchestrator-go/cmd
go run main.go -profile receptionist
```

The `-profile` flag accepts the name of any YAML file present in
`configs/agent_profiles/` without the `.yaml` extension.

Available profiles out of the box:

- `receptionist` — Sarah, front desk agent for Smile Dental Clinic

Output audio is written to `test_data/output.raw` as raw PCM bytes.

Always start the inference engine before starting the orchestrator.

---

## Session State Machine

Every call moves through a strict lifecycle. Illegal transitions are
caught and logged — they never silently corrupt state.

```text
CREATED
   ↓
CONNECTING     gRPC stream established
   ↓
ACTIVE         stream open, user audio flowing
   ↓
PROCESSING     all user audio sent, inference running
   ↓
RESPONDING     inference engine streaming audio back
   ↓
TERMINATED     stream closed, resources released
```

---

## Project Structure

```text
orchestrator-go/
├── cmd/
│   └── main.go           # Entry point
├── internal/
│   ├── config/
│   │   └── config.go     # Agent profile YAML loader
│   └── session/
│       └── session.go    # Session state machine and goroutine pumps
├── generated/            # Generated protobuf and gRPC bindings
├── go.mod
└── go.sum
```

---

## Adding a New Agent Profile

Create a YAML file in `configs/agent_profiles/`:

```yaml
name: "Your Agent Name"
description: "What this agent does."
system_prompt: |
  You are ... describe the persona and constraints here.
```

Run the orchestrator with `-profile your_filename` and the inference engine
will adopt that persona without any code changes.
