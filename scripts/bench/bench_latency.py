import argparse
import csv
import datetime
import json
import os
import platform
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AETHER_ROOT = os.path.abspath(os.path.join(REPO_ROOT, "..", "AetherRTC"))
INFERENCE_DIR = os.path.join(REPO_ROOT, "services", "inference-py")
ORCH_DIR = os.path.join(REPO_ROOT, "services", "orchestrator-go")
UTTERANCE_DIR = os.path.join(REPO_ROOT, "test_data", "bench")
RESULT_DIR = os.path.join(REPO_ROOT, "docs", "benchmarks", "latest")
INFERENCE_PY = os.path.join(INFERENCE_DIR, ".venv", "Scripts", "python.exe")

GATEWAY_ADDR = "127.0.0.1:50052"
INFERENCE_PORT = 50051
GATEWAY_PORT = 50052
LLM_MODEL = "qwen2.5:3b"

SAMPLE_RATE = 8000
FRAME_BYTES = 320
FRAME_S = 0.020
PREROLL_FRAMES = 15
INTER_TURN_PAUSE_S = 1.0
RESPONSE_QUIET_S = 0.3

COLD_UTTERANCES = ["u02", "u03", "u05"]
WARMUP_UTTERANCES = ["u02", "u05"]
LONG_TIMEOUT_S = 180.0


def run(cmd, cwd=None, timeout=60):
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return (out.stdout + out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"ERROR: {e}"


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        capture_output=True,
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def percentile_table(values):
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
    }


def normalize_words(text):
    return re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split()


def word_error_rate(reference, hypothesis):
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        return None
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)] / len(ref)


def load_utterances():
    with open(os.path.join(UTTERANCE_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    utterances = {}
    for item in manifest:
        with wave.open(os.path.join(UTTERANCE_DIR, item["id"] + ".wav"), "rb") as w:
            assert w.getframerate() == SAMPLE_RATE and w.getsampwidth() == 2
            pcm = w.readframes(w.getnframes())
        utterances[item["id"]] = {"text": item["text"], "pcm": pcm}
    return utterances


def to_frames(pcm):
    padded = pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)
    return [padded[i : i + FRAME_BYTES] for i in range(0, len(padded), FRAME_BYTES)]


def load_gateway_stubs():
    stub_dir = tempfile.mkdtemp(prefix="var_bench_stubs_")
    proto_dir = os.path.join(REPO_ROOT, "proto")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{proto_dir}",
            f"--python_out={stub_dir}",
            f"--grpc_python_out={stub_dir}",
            os.path.join(proto_dir, "gateway.proto"),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"protoc failed: {result.stderr}")
    sys.path.insert(0, stub_dir)
    import gateway_pb2
    import gateway_pb2_grpc

    return gateway_pb2, gateway_pb2_grpc


class LogTail(threading.Thread):
    ERROR_PATTERN = re.compile(r"Traceback|Exception in thread|\[ERROR\]|\[CRITICAL\]")

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.bench_q = queue.Queue()
        self.error_lines = []
        self._ttft_pending = None
        self._halt = threading.Event()
        self._markers = {}
        self._lock = threading.Lock()

    def wait_for(self, marker, timeout):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self._lock:
                if self._markers.get(marker) is not None:
                    return self._markers[marker]
            time.sleep(0.05)
        return None

    def watch(self, marker):
        with self._lock:
            self._markers.setdefault(marker, None)

    def run(self):
        while not os.path.exists(self.path) and not self._halt.is_set():
            time.sleep(0.05)
        with open(self.path, "rb") as f:
            while not self._halt.is_set():
                raw = f.readline()
                if not raw:
                    time.sleep(0.005)
                    continue
                self._handle(raw.decode("utf-8", errors="replace").rstrip())

    def _handle(self, line):
        now = time.perf_counter()
        with self._lock:
            for marker, seen in list(self._markers.items()):
                if seen is None and marker in line:
                    self._markers[marker] = now
        match = re.search(r"TTFT: ([0-9.]+)s", line)
        if match:
            self._ttft_pending = float(match.group(1))
        bench = re.search(r"BENCH (\{.*\})", line)
        if bench:
            record = json.loads(bench.group(1))
            record["ttft_s"] = self._ttft_pending
            self._ttft_pending = None
            self.bench_q.put((now, record))
        elif self.ERROR_PATTERN.search(line):
            self.error_lines.append(line[:300])

    def stop(self):
        self._halt.set()


class Stack:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.inference = None
        self.orchestrator = None
        self.tail = None
        self._log_handles = []
        self.exe = os.path.join(
            tempfile.mkdtemp(prefix="var_bench_build_"), "gateway-server.exe"
        )

    def build_orchestrator(self):
        result = subprocess.run(
            ["go", "build", "-o", self.exe, "./cmd/gateway-server"],
            cwd=ORCH_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"go build failed: {result.stderr}")

    def start(self, tag):
        for port in (INFERENCE_PORT, GATEWAY_PORT):
            if port_open(port):
                raise RuntimeError(
                    f"port {port} is already in use; stop the running stack first"
                )

        inference_log = os.path.join(self.out_dir, f"inference_{tag}.log")
        orch_log = os.path.join(self.out_dir, f"orchestrator_{tag}.log")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        env.pop("VIRTUAL_ENV", None)

        self.tail = LogTail(inference_log)
        self.tail.watch("Inference Engine running on port")
        self.tail.start()

        spawn = time.perf_counter()
        log_a = open(inference_log, "wb")
        self.inference = subprocess.Popen(
            [INFERENCE_PY, "-u", "main.py"],
            cwd=INFERENCE_DIR,
            stdout=log_a,
            stderr=subprocess.STDOUT,
            env=env,
        )
        ready_at = self.tail.wait_for("Inference Engine running on port", 180)
        if ready_at is None:
            raise RuntimeError("inference engine did not become ready in 180s")
        inference_ready_s = ready_at - spawn

        log_b = open(orch_log, "wb")
        self._log_handles += [log_a, log_b]
        self.orchestrator = subprocess.Popen(
            [self.exe, "-profile", "receptionist", "-port", ":50052",
             "-inference", f"localhost:{INFERENCE_PORT}"],
            cwd=ORCH_DIR,
            stdout=log_b,
            stderr=subprocess.STDOUT,
        )
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline and not port_open(GATEWAY_PORT):
            time.sleep(0.05)
        if not port_open(GATEWAY_PORT):
            raise RuntimeError("orchestrator did not open :50052 in 30s")
        return {"inference_ready_s": inference_ready_s}

    def stop(self):
        kill_tree(self.orchestrator)
        kill_tree(self.inference)
        if self.tail:
            self.tail.stop()
        for handle in self._log_handles:
            handle.close()
        self._log_handles = []
        self.orchestrator = self.inference = self.tail = None


class GpuSampler:
    def __init__(self, path):
        self.path = path
        self.proc = None
        self.handle = None

    def start(self):
        self.handle = open(self.path, "wb")
        self.proc = subprocess.Popen(
            [
                "nvidia-smi",
                "--query-gpu=timestamp,memory.used,utilization.gpu,power.draw",
                "--format=csv,noheader,nounits",
                "-lms",
                "200",
            ],
            stdout=self.handle,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        kill_tree(self.proc)
        if self.handle:
            self.handle.close()
        rows = []
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 4:
                    continue
                try:
                    mem, util = float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                try:
                    power = float(parts[3])
                except ValueError:
                    power = 0.0
                rows.append((mem, util, power))
        if not rows:
            return {"samples": 0}
        mem = [r[0] for r in rows]
        return {
            "samples": len(rows),
            "mem_used_mib_peak": max(mem),
            "mem_used_mib_first": mem[0],
            "gpu_util_pct_peak": max(r[1] for r in rows),
            "power_w_peak": max(r[2] for r in rows),
        }


class Call:
    def __init__(self, pb, pb_grpc, session_id):
        import grpc

        self.pb = pb
        self.session_id = session_id
        self._out = queue.Queue()
        self.rx = queue.Queue()
        self.rx_error = None
        self._channel = grpc.insecure_channel(GATEWAY_ADDR)
        stub = pb_grpc.GatewayStub(self._channel)
        self._stream = stub.StreamAudio(self._requests())
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._out.put(
            pb.GatewayEvent(
                session_id=session_id,
                control=pb.GatewayControl(
                    type=pb.GatewayControl.START_SESSION,
                    source_sample_rate=SAMPLE_RATE,
                ),
            )
        )
        self._next_frame_at = time.perf_counter()

    def _requests(self):
        while True:
            item = self._out.get()
            if item is None:
                return
            yield item

    def _read(self):
        import grpc

        try:
            for event in self._stream:
                if event.HasField("audio"):
                    self.rx.put((time.perf_counter(), len(event.audio.data)))
        except grpc.RpcError as e:
            self.rx_error = e

    def send_frame(self, frame):
        now = time.perf_counter()
        wait = self._next_frame_at - now
        if wait > 0:
            time.sleep(wait)
        elif wait < -0.1:
            self._next_frame_at = now
        self._next_frame_at += FRAME_S
        self._out.put(
            self.pb.GatewayEvent(
                session_id=self.session_id,
                audio=self.pb.AudioChunk(data=frame),
            )
        )

    def resync_clock(self):
        self._next_frame_at = time.perf_counter()

    def drain_rx(self):
        dropped = 0
        while True:
            try:
                self.rx.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped

    def close(self):
        self._out.put(
            self.pb.GatewayEvent(
                session_id=self.session_id,
                control=self.pb.GatewayControl(type=self.pb.GatewayControl.END_SESSION),
            )
        )
        self._out.put(None)
        time.sleep(0.2)
        self._stream.cancel()
        self._channel.close()


SILENCE_FRAME = b"\x00" * FRAME_BYTES


def playback_gaps(chunks):
    gaps = []
    plays_until = None
    for at, nbytes in chunks:
        if plays_until is None or at > plays_until:
            if plays_until is not None:
                gaps.append(at - plays_until)
            plays_until = at
        plays_until += nbytes / 2 / SAMPLE_RATE
    return gaps


def run_turn(call, tail, utterance, turn_timeout):
    stale = 0
    while True:
        try:
            tail.bench_q.get_nowait()
            stale += 1
        except queue.Empty:
            break
    stale_audio = call.drain_rx()

    for _ in range(PREROLL_FRAMES):
        call.send_frame(SILENCE_FRAME)
    for frame in to_frames(utterance["pcm"]):
        call.send_frame(frame)
    t_end_of_speech = time.perf_counter()

    bench_events = []
    first_audio_at = None
    status = "no_response"
    deadline = t_end_of_speech + turn_timeout

    while time.perf_counter() < deadline:
        try:
            first_audio_at, first_bytes = call.rx.get_nowait()
            break
        except queue.Empty:
            pass
        try:
            bench_events.append(tail.bench_q.get_nowait())
            if bench_events[-1][1].get("first_audio_s") is None:
                status = "no_audio"
                break
        except queue.Empty:
            pass
        call.send_frame(SILENCE_FRAME)

    total_bytes = 0
    last_rx_at = None
    chunks = []
    if first_audio_at is not None:
        total_bytes = first_bytes
        last_rx_at = first_audio_at
        chunks.append((first_audio_at, first_bytes))
        status = "ok"
        completion_deadline = time.perf_counter() + 90
        while time.perf_counter() < completion_deadline:
            try:
                at, n = call.rx.get(timeout=0.05)
                total_bytes += n
                last_rx_at = at
                chunks.append((at, n))
            except queue.Empty:
                pass
            try:
                bench_events.append(tail.bench_q.get_nowait())
            except queue.Empty:
                pass
            if bench_events and time.perf_counter() - last_rx_at > RESPONSE_QUIET_S:
                break
        else:
            status = "no_completion"

    call.resync_clock()
    for _ in range(int(INTER_TURN_PAUSE_S / FRAME_S)):
        call.send_frame(SILENCE_FRAME)

    record = {
        "status": status,
        "stale_bench_events": stale,
        "stale_audio_chunks": stale_audio,
        "speech_s": len(utterance["pcm"]) / 2 / SAMPLE_RATE,
        "bench_events": len(bench_events),
    }
    if status == "ok" and len(bench_events) > 1:
        record["status"] = "split"
    if stale and record["status"] == "ok":
        record["status"] = "desync"

    if first_audio_at is not None:
        record["e2e_first_audio_s"] = first_audio_at - t_end_of_speech
        record["response_total_s"] = last_rx_at - t_end_of_speech
        record["response_audio_s"] = total_bytes / 2 / SAMPLE_RATE
        gaps = playback_gaps(chunks)
        record["max_playback_gap_s"] = max(gaps) if gaps else 0.0
        record["playback_gaps_over_300ms"] = sum(1 for g in gaps if g > 0.3)
        record["playback_gaps_over_400ms"] = sum(1 for g in gaps if g > 0.4)
    if bench_events:
        b = bench_events[0][1]
        record.update(
            server_stt_s=b.get("stt_s"),
            server_ttft_s=b.get("ttft_s"),
            server_first_sentence_s=b.get("first_sentence_s"),
            server_first_audio_s=b.get("first_audio_s"),
            server_total_s=b.get("total_s"),
            tts_synth_s=b.get("tts_s"),
            tts_audio_s=b.get("tts_audio_s"),
            sentences=b.get("sentences"),
            stt_text=b.get("stt_text"),
            completed=b.get("completed"),
        )
        record["wer"] = word_error_rate(utterance["text"], b.get("stt_text"))
        if b.get("sentences"):
            record["tts_per_sentence_s"] = b["tts_s"] / b["sentences"]
    return record


def run_call(
    pb, pb_grpc, tail, session_id, utterance_ids, utterances, args, meta, timeout=None
):
    call = Call(pb, pb_grpc, session_id)
    rows = []
    consecutive_failures = 0
    aborted = False
    try:
        for idx, utt_id in enumerate(utterance_ids, 1):
            record = run_turn(
                call, tail, utterances[utt_id], timeout or args.turn_timeout
            )
            record.update(meta)
            record.update(
                session_id=session_id,
                turn_in_session=idx,
                utterance_id=utt_id,
                reference_text=utterances[utt_id]["text"],
            )
            rows.append(record)
            ok = record["status"] in ("ok", "split", "desync")
            consecutive_failures = 0 if ok else consecutive_failures + 1
            print(
                f"  {session_id} turn {idx:2d} {utt_id} {record['status']:<11} "
                f"e2e={record.get('e2e_first_audio_s', float('nan')):.2f}s",
                flush=True,
            )
            if consecutive_failures >= args.max_consecutive_failures:
                aborted = True
                print(f"  {session_id}: aborting after {consecutive_failures} failures")
                break
    finally:
        call.close()
    return rows, aborted


def read_ollama_ps():
    return run(["ollama", "ps"])


def unload_model():
    run(["ollama", "stop", LLM_MODEL])
    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline:
        if LLM_MODEL not in read_ollama_ps():
            return True
        time.sleep(0.5)
    return False


def git_state(path):
    return {
        "commit": run(["git", "rev-parse", "HEAD"], cwd=path),
        "branch": run(["git", "branch", "--show-current"], cwd=path),
        "dirty_files": run(["git", "status", "--porcelain"], cwd=path).splitlines(),
    }


def vad_min_silence_ms():
    with open(os.path.join(INFERENCE_DIR, "main.py"), encoding="utf-8") as f:
        match = re.search(r"min_silence_duration=(\d+)", f.read())
    return int(match.group(1)) if match else 0


def read_ac_power():
    return run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus "
         "-ErrorAction SilentlyContinue).PowerOnline"]
    )


def capture_environment():
    def source_default(path, pattern):
        with open(path, encoding="utf-8") as f:
            match = re.search(pattern, f.read())
        return match.group(1) if match else None

    main_src = os.path.join(INFERENCE_DIR, "main.py")
    gpu = run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
         "--format=csv,noheader"]
    )
    smi = run(["nvidia-smi"])
    cuda = re.search(r"CUDA Version: ([0-9.]+)", smi)
    ollama_show = run(["ollama", "show", LLM_MODEL])
    quant = re.search(r"quantization\s+(\S+)", ollama_show)
    params = re.search(r"parameters\s+(\S+)", ollama_show)
    power = read_ac_power()
    cpu = run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Processor).Name"]
    )
    ram = run(
        ["powershell", "-NoProfile", "-Command",
         "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"]
    )
    return {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "os": platform.platform(),
        "cpu": cpu,
        "ram_gb": ram,
        "gpu": gpu,
        "cuda_version_driver_max": cuda.group(1) if cuda else None,
        "gpu_compute_apps": run(
            ["nvidia-smi", "--query-compute-apps=pid,name,used_memory",
             "--format=csv,noheader"]
        ).replace(os.path.expanduser("~"), "~"),
        "ac_power_online": power,
        "power_scheme": run(["powercfg", "/getactivescheme"]),
        "python": run([INFERENCE_PY, "--version"]),
        "go": run(["go", "version"]),
        "llm_model": LLM_MODEL,
        "llm_parameters": params.group(1) if params else None,
        "llm_quantization": quant.group(1) if quant else None,
        "ollama_version": run(["ollama", "--version"]),
        "stt": {
            "model_size": source_default(
                os.path.join(INFERENCE_DIR, "stt", "transcriber.py"),
                r'model_size: str = "([^"]+)"',
            ),
            "device": source_default(
                os.path.join(INFERENCE_DIR, "stt", "transcriber.py"),
                r'device: str = "([^"]+)"',
            ),
            "compute_type": source_default(
                os.path.join(INFERENCE_DIR, "stt", "transcriber.py"),
                r'compute_type: str = "([^"]+)"',
            ),
        },
        "tts_model": "en_US-lessac-medium.onnx (Piper, CPU)",
        "vad_model": "silero_vad.onnx (onnxruntime, CPU)",
        "vad_min_silence_ms": int(
            source_default(main_src, r"min_silence_duration=(\d+)") or 0
        ),
        "repos": {
            "voice-agent-runtime": git_state(REPO_ROOT),
            "AetherRTC": git_state(AETHER_ROOT),
        },
    }


def summarize(rows):
    ok = [r for r in rows if r["status"] == "ok"]
    metrics = [
        "e2e_first_audio_s",
        "response_total_s",
        "response_audio_s",
        "server_stt_s",
        "server_ttft_s",
        "server_first_sentence_s",
        "server_first_audio_s",
        "server_total_s",
        "tts_synth_s",
        "tts_per_sentence_s",
        "tts_audio_s",
        "max_playback_gap_s",
        "wer",
    ]
    out = {m: percentile_table([r.get(m) for r in ok]) for m in metrics}
    out["turns_with_gap_over_300ms"] = sum(
        1 for r in ok if r.get("playback_gaps_over_300ms")
    )
    out["turns_with_gap_over_400ms"] = sum(
        1 for r in ok if r.get("playback_gaps_over_400ms")
    )
    synth = sum(r["tts_synth_s"] for r in ok if r.get("tts_synth_s"))
    audio = sum(r["tts_audio_s"] for r in ok if r.get("tts_audio_s"))
    out["tts_rtf_aggregate"] = audio / synth if synth else None
    out["tts_rtf_per_turn"] = percentile_table(
        [
            r["tts_audio_s"] / r["tts_synth_s"]
            for r in ok
            if r.get("tts_synth_s") and r.get("tts_audio_s")
        ]
    )
    out["counts"] = {
        s: sum(1 for r in rows if r["status"] == s)
        for s in sorted({r["status"] for r in rows})
    }
    out["attempted"] = len(rows)
    return out


def write_turns_csv(rows, path):
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def warm_run(args, stack, utterances, pb, pb_grpc, out_dir, run_index):
    print(f"== warm run {run_index}/{args.warm_runs} ==")
    stack.build_orchestrator()
    model_unloaded = unload_model()
    sampler = GpuSampler(os.path.join(out_dir, f"gpu_warm{run_index}.csv"))
    sampler.start()
    start_info = stack.start(f"warm{run_index}")
    ids = sorted(utterances)

    print(" warm-up (excluded from statistics)")
    warmup_rows, _ = run_call(
        pb, pb_grpc, stack.tail, f"warmup{run_index}", WARMUP_UTTERANCES, utterances,
        args, {"mode": "warmup", "warm_run": run_index}, timeout=LONG_TIMEOUT_S,
    )
    ollama_ps_after_warmup = read_ollama_ps()

    rows, aborted_sessions = [], []
    for p in range(1, args.passes + 1):
        order = ids[:]
        random.Random(args.seed + 1000 * run_index + p).shuffle(order)
        session_id = f"warm{run_index}-{p}"
        print(f" pass {p}/{args.passes}")
        got, aborted = run_call(
            pb, pb_grpc, stack.tail, session_id, order, utterances, args,
            {"mode": "warm", "warm_run": run_index, "pass": p},
        )
        rows += got
        if aborted:
            aborted_sessions.append(session_id)

    gpu = sampler.stop()
    gpu["model_unloaded_before_start"] = model_unloaded
    errors = list(stack.tail.error_lines)
    stack.stop()
    return {
        "rows": rows,
        "warmup_rows": warmup_rows,
        "summary": summarize(rows),
        "aborted_sessions": aborted_sessions,
        "gpu": gpu,
        "ollama_ps_after_warmup": ollama_ps_after_warmup,
        "inference_ready_s": start_info["inference_ready_s"],
        "server_error_lines": errors,
    }


def pool_warm(runs):
    rows = [r for run in runs for r in run["rows"]]
    gpus = [run["gpu"] for run in runs]
    return {
        "rows": rows,
        "warmup_rows": [r for run in runs for r in run["warmup_rows"]],
        "summary": summarize(rows),
        "per_run": [
            {
                "run": i,
                "summary": run["summary"],
                "aborted_sessions": run["aborted_sessions"],
                "inference_ready_s": run["inference_ready_s"],
                "gpu": run["gpu"],
            }
            for i, run in enumerate(runs, 1)
        ],
        "aborted_sessions": [s for run in runs for s in run["aborted_sessions"]],
        "gpu": {
            "samples": sum(g.get("samples", 0) for g in gpus),
            "mem_used_mib_first": gpus[0].get("mem_used_mib_first"),
            "mem_used_mib_peak": max(g.get("mem_used_mib_peak", 0) for g in gpus),
            "gpu_util_pct_peak": max(g.get("gpu_util_pct_peak", 0) for g in gpus),
            "power_w_peak": max(g.get("power_w_peak", 0) for g in gpus),
            "model_unloaded_before_start": all(
                g.get("model_unloaded_before_start") for g in gpus
            ),
        },
        "ollama_ps_after_warmup": runs[0]["ollama_ps_after_warmup"],
        "server_error_lines": [e for run in runs for e in run["server_error_lines"]],
    }


def cold_run(args, stack, utterances, pb, pb_grpc, out_dir):
    print("== cold-start run ==")
    stack.build_orchestrator()
    sampler = GpuSampler(os.path.join(out_dir, "gpu_cold.csv"))
    sampler.start()
    rows, ready, unloaded_ok, errors = [], [], [], []
    for r in range(1, args.cold_repeats + 1):
        stack.stop()
        unloaded_ok.append(unload_model())
        print(f" cold repeat {r}/{args.cold_repeats} (model unloaded: {unloaded_ok[-1]})")
        info = stack.start(f"cold{r}")
        ready.append(info["inference_ready_s"])
        got, _ = run_call(
            pb, pb_grpc, stack.tail, f"cold-{r}", COLD_UTTERANCES, utterances, args,
            {"mode": "cold", "repeat": r}, timeout=LONG_TIMEOUT_S,
        )
        rows += got
        errors += stack.tail.error_lines
        stack.stop()
    gpu = sampler.stop()
    by_turn = {}
    for idx in range(1, len(COLD_UTTERANCES) + 1):
        subset = [r for r in rows if r["turn_in_session"] == idx]
        by_turn[str(idx)] = summarize(subset)
    return {
        "rows": rows,
        "summary_by_turn": by_turn,
        "gpu": gpu,
        "inference_ready_s": percentile_table(ready),
        "model_unloaded_before_each_repeat": unloaded_ok,
        "server_error_lines": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Reproducible VAR latency benchmark")
    parser.add_argument("mode", choices=["warm", "cold", "all"])
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--warm-runs", type=int, default=3)
    parser.add_argument("--cold-repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--turn-timeout", type=float, default=45.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=1)
    parser.add_argument("--out", default=RESULT_DIR)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    utterances = load_utterances()
    pb, pb_grpc = load_gateway_stubs()
    stack = Stack(args.out)

    result = {
        "environment": capture_environment(),
        "parameters": {
            "passes": args.passes,
            "warm_runs": args.warm_runs,
            "cold_repeats": args.cold_repeats,
            "seed": args.seed,
            "turn_timeout_s": args.turn_timeout,
            "utterances": {k: v["text"] for k, v in utterances.items()},
            "inter_turn_pause_s": INTER_TURN_PAUSE_S,
            "preroll_s": PREROLL_FRAMES * FRAME_S,
        },
    }
    try:
        if args.mode in ("cold", "all"):
            result["cold"] = cold_run(args, stack, utterances, pb, pb_grpc, args.out)
            write_turns_csv(result["cold"]["rows"], os.path.join(args.out, "turns_cold.csv"))
        if args.mode in ("warm", "all"):
            runs = [
                warm_run(args, stack, utterances, pb, pb_grpc, args.out, i)
                for i in range(1, args.warm_runs + 1)
            ]
            result["warm"] = pool_warm(runs)
            write_turns_csv(result["warm"]["rows"], os.path.join(args.out, "turns_warm.csv"))
    finally:
        stack.stop()

    result["environment"]["ac_power_online_end"] = read_ac_power()
    summary_path = os.path.join(args.out, "latency.json")
    existing = {}
    if os.path.exists(summary_path) and args.mode != "all":
        with open(summary_path, encoding="utf-8") as f:
            existing = json.load(f)
    existing.update(result)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    print(f"wrote {summary_path}")

    report = os.path.join(os.path.dirname(__file__), "make_report.py")
    command = [sys.executable, report, "--data", args.out]
    if os.path.abspath(args.out) != os.path.abspath(RESULT_DIR):
        command += ["--out", os.path.join(args.out, "benchmarks.md")]
    subprocess.run(command, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
