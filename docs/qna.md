### 1. "Why gRPC over REST?"

**The Answer:**
"For a real-time voice agent, REST is fundamentally the wrong paradigm. REST is stateless and heavily relies on the Request-Response lifecycle. If I used REST, I would be forced into a 'batching' architecture—waiting for the user to finish speaking, sending a massive audio payload, and waiting for a massive response.

I chose gRPC specifically for its **native bidirectional streaming** over HTTP/2. It allows the Go orchestrator and the Python inference engine to maintain a persistent, open connection. I can stream user audio chunks up to Python while simultaneously streaming synthesized AI audio bytes down to Go. Furthermore, using Protocol Buffers allowed me to define a strict, strongly-typed contract (the `Event` proto with a `oneof` payload). This eliminates the serialization overhead of JSON and guarantees that the Go control plane and Python data plane never misinterpret payload boundaries, which is critical when piping raw byte buffers."

- **The Senior Flex:** You demonstrated that you understand the difference between half-duplex (REST) and full-duplex (gRPC streams), and highlighted the performance benefits of Protobufs over JSON for binary data.

### 2. "Why Go for orchestration?"

**The Answer:**
"Orchestrating voice calls is an inherently I/O-bound and highly concurrent problem. Every active phone call needs to be isolated, and inside each call, we are simultaneously reading network packets, writing network packets, and managing state.

If I used Python for orchestration, I’d have to fight the Global Interpreter Lock (GIL) or deal with the heavy memory footprint of multi-processing. Go was the perfect choice because goroutines have a negligible memory footprint (starting around 2KB) compared to OS threads. I can spin up a dedicated `Session`, a `readPump`, and a `writePump` for thousands of concurrent calls on a single node without breaking a sweat. More importantly, Go’s channels provide a thread-safe way to decouple my I/O layer from my business logic. The network layer just drops bytes into the `UserAudioChan`, completely ignorant of the underlying gRPC stream or state machine."

- **The Senior Flex:** You contrasted Go's concurrency primitives with Python's limitations, showing you use the right tool for the right job (Python for ML, Go for concurrent I/O).

### 3. "How does your session state machine prevent race conditions?"

**The Answer:**
"Race conditions are the biggest risk in a dual-pump streaming architecture because you have concurrent goroutines reading and writing simultaneously. For example, the user might hang up at the exact millisecond the AI sends its first audio chunk.

To prevent race conditions, I stripped state mutation privileges away from the individual goroutines. The state is protected by a `sync.Mutex` and can only be mutated through a centralized `transitionTo()` method. Inside that method, the requested state is validated against a strict, pre-defined legal transition map (e.g., `ACTIVE` can only transition to `PROCESSING` or `TERMINATED`). If `readPump` and `writePump` try to alter the state at the exact same time, the mutex queues the requests, and the transition map ensures that whichever request arrives second is either gracefully ignored or logged as an illegal transition, preventing the session from silently entering a corrupted state."

- **The Senior Flex:** You didn't just say "I used a lock." You explained the concept of a finite state machine (FSM) and how combining a mutex with a legal transition map guarantees deterministic behavior.

### 4. "What happens when the LLM fails mid-stream?"

**The Answer:**
"In a distributed architecture, failure containment is critical. If the LLM process crashes or hangs mid-stream, the blast radius is strictly confined to that single session.

On the Python side, the inference pipeline is wrapped in local error boundaries. If Ollama throws an exception, the Python servicer catches it, logs the trace, yields a fallback audio chunk or control signal, and gracefully closes the stream rather than crashing the gRPC server.

On the Go side, the `readPump` immediately detects the stream EOF or error. It signals the transition to `TERMINATED`, closes the `AgentAudioChan`, and triggers the `DoneChan`. Because the Go orchestrator uses channels to communicate, the closing of these channels naturally cascades to tear down the session's goroutines. The overall Go platform remains entirely stable, and all other hundreds of concurrent calls continue running without noticing the failure."

- **The Senior Flex:** You demonstrated "Blast Radius" awareness. You showed that you designed the system assuming that downstream services _will_ fail, and you built the pipelines to tear down gracefully rather than leaking memory or panicking the main process.
