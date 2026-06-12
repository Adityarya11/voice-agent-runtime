# Inference Engine — Python

The inference service is the AI backbone of the voice runtime. It receives
audio from the orchestrator over a gRPC bidirectional stream, runs it through
a three-stage pipeline, and streams synthesized audio back.

---

## Pipeline

```text
Audio Chunks (gRPC)
        ↓
Speech to Text — Faster-Whisper (CPU, int8)
        ↓
Language Model — qwen2.5:3b via Ollama (GPU)
        ↓
Text to Speech — Piper TTS (CPU)
        ↓
Audio Chunks (gRPC)
```

---

## Requirements

**Runtime dependencies**

- Python 3.11
- [uv](https://github.com/astral-sh/uv) for environment and package management
- [Ollama](https://ollama.com) running locally with `qwen2.5:3b` pulled
- Piper ONNX voice model file placed at `models/en_US-lessac-medium.onnx`

**Hardware expectations**

- LLM runs on GPU via Ollama. An RTX 3050 Mobile (4GB VRAM) is sufficient.
- STT and TTS run on CPU. Any modern multi-core CPU handles both in
  near real-time.

---

## Setup

Install dependencies:

```bash
cd services/inference-py
uv sync
```

Pull the language model:

```bash
ollama pull qwen2.5:3b
```

Download the Piper voice model and place it:

```bash
mkdir -p models
# Download en_US-lessac-medium.onnx from
# https://github.com/rhasspy/piper/releases
# and place it at services/inference-py/models/en_US-lessac-medium.onnx
```

---

## Running

```bash
cd services/inference-py
uv run python main.py
```

The server starts on port `50051` and waits for the orchestrator to connect.
Start the inference engine before starting the orchestrator.

---

## Agent Profiles

The inference engine does not need to know about agent profiles ahead of
time. The orchestrator sends the active profile's system prompt inside the
`ControlSignal` at the start of every session. The LLM adopts that persona
for the duration of the call.

If no profile is received, the engine falls back to a generic concise voice
assistant prompt.

---

## Project Structure

```text
inference-py/
├── grpc_server/          # Generated protobuf and gRPC bindings
├── stt/
│   └── transcriber.py    # Faster-Whisper wrapper
├── llm/
│   └── engine.py         # Ollama inference wrapper
├── tts/
│   └── synthesizer.py    # Piper TTS wrapper
├── models/               # ONNX voice model files (not committed)
├── main.py               # gRPC server entry point
├── pyproject.toml
└── requirements.txt
```
