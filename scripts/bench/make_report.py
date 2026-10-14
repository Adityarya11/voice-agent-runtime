import argparse
import collections
import json
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DATA = os.path.join(REPO_ROOT, "docs", "benchmarks", "latest")
DEFAULT_OUT = os.path.join(REPO_ROOT, "docs", "benchmarks.md")

COLUMNS = ["n", "min", "p50", "p95", "p99", "max", "mean"]


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cell(table, key, scale, digits):
    if not table or key not in table:
        return "-"
    value = table[key]
    return str(value) if key == "n" else f"{value * scale:.{digits}f}"


def stat_row(label, table, scale=1.0, digits=3):
    return "| " + label + " | " + " | ".join(
        cell(table, c, scale, digits) for c in COLUMNS
    ) + " |"


def stat_header(unit):
    return (
        f"| Metric ({unit}) | n | min | p50 | p95 | p99 | max | mean |\n"
        "|---|---|---|---|---|---|---|---|"
    )


def scalar(table, key, scale=1.0, digits=3):
    if not table or key not in table:
        return None
    return table[key] * scale


def verdict(measured, low, high):
    if measured is None:
        return "not measured"
    if low <= measured <= high:
        return "holds"
    return "contradicted" if measured > high else "better than claimed"


def environment_section(env):
    repos = env["repos"]
    lines = ["## Environment", "", "| Item | Value |", "|---|---|"]
    rows = [
        ("Run timestamp", env["timestamp"]),
        ("OS", env["os"]),
        ("CPU", env["cpu"]),
        ("RAM (GB)", env["ram_gb"]),
        ("GPU, driver, VRAM", env["gpu"]),
        ("CUDA version (driver maximum, from nvidia-smi)", env["cuda_version_driver_max"]),
        ("AC power online (start / end)", f"{env['ac_power_online']} / {env.get('ac_power_online_end', '-')}"),
        ("Power scheme", env["power_scheme"]),
        ("Other GPU compute apps at start", env["gpu_compute_apps"] or "none reported"),
        ("LLM", f"{env['llm_model']} ({env['llm_parameters']}, {env['llm_quantization']}) via Ollama {env['ollama_version']}"),
        ("STT", f"faster-whisper {env['stt']['model_size']}, device {env['stt']['device']}, {env['stt']['compute_type']}"),
        ("TTS", env["tts_model"]),
        ("VAD", f"{env['vad_model']}, min silence {env['vad_min_silence_ms']} ms (set in services/inference-py/main.py)"),
        ("Python / Go", f"{env['python']} / {env['go']}"),
    ]
    for key, value in rows:
        lines.append(f"| {key} | {str(value).replace(chr(10), ' ')} |")
    for name, state in repos.items():
        dirty = state["dirty_files"]
        lines.append(
            f"| {name} commit | `{state['commit']}` on `{state['branch']}`, "
            f"{len(dirty)} uncommitted file(s) |"
        )
    lines += ["", "Uncommitted files at run time (results come from this working tree, not a clean commit):", ""]
    for name, state in repos.items():
        for entry in state["dirty_files"]:
            lines.append(f"- {name}: `{entry}`")
    lines.append("")
    return lines


def group_errors(error_lines):
    counts = collections.Counter()
    for line in error_lines:
        message = re.sub(r"^\[[^\]]*\]\s*", "", line)
        message = re.sub(r"\[[^\]]*\]", "", message, count=3).strip()
        counts[message[:110]] += 1
    return counts


def warm_section(latency):
    warm = latency.get("warm")
    if not warm:
        return []
    s = warm["summary"]
    params = latency["parameters"]
    runs = params.get("warm_runs", 1)
    lines = [
        "## Warm run",
        "",
        f"{runs} independent runs. Each run: model unloaded, fresh processes, 2 excluded warm-up turns, then "
        f"{params['passes']} calls of {len(params['utterances'])} utterances each, shuffled per call with a fixed "
        f"seed ({params['seed']}). Each call is one session, so conversation history grows to its 12-message cap "
        "as in a real call. All statistics below pool every run.",
        "",
        f"Turn outcomes: {json.dumps(s['counts'])} out of {s['attempted']} attempted. "
        f"A failed turn ends its session; sessions ended that way: {warm['aborted_sessions'] or 'none'}. "
        "Statistics use turns with status `ok` only.",
        "",
    ]
    failed = [r for r in warm["rows"] if r["status"] != "ok"]
    if failed:
        lines += ["Turns that did not complete:", ""]
        for r in failed:
            lines.append(
                f"- session `{r['session_id']}`, turn {r['turn_in_session']}, utterance `{r['utterance_id']}` "
                f"({r['speech_s']:.2f} s of speech): `{r['status']}`, server BENCH events: {r['bench_events']}"
            )
        lines.append("")
    per_run = warm.get("per_run")
    if per_run:
        lines += [
            "Run-to-run spread (the same protocol repeated; this is how much to trust a single number):",
            "",
            "| Run | ok / attempted | e2e first audio p50 (s) | e2e p95 (s) | server STT p50 (s) | TTFT p50 (s) | server first audio p50 (s) |",
            "|---|---|---|---|---|---|---|",
        ]
        for pr in per_run:
            ps = pr["summary"]
            lines.append(
                f"| {pr['run']} | {ps['counts'].get('ok', 0)} / {ps['attempted']} | "
                f"{cell(ps['e2e_first_audio_s'], 'p50', 1, 3)} | {cell(ps['e2e_first_audio_s'], 'p95', 1, 3)} | "
                f"{cell(ps['server_stt_s'], 'p50', 1, 3)} | {cell(ps['server_ttft_s'], 'p50', 1, 3)} | "
                f"{cell(ps['server_first_audio_s'], 'p50', 1, 3)} |"
            )
        lines.append("")
    lines += [
        stat_header("seconds"),
        stat_row("End of speech to first audio byte at client", s["e2e_first_audio_s"]),
        stat_row("End of speech to last audio byte at client", s["response_total_s"]),
        stat_row("Server: STT", s["server_stt_s"]),
        stat_row("Server: LLM time to first token", s["server_ttft_s"]),
        stat_row("Server: STT start to first LLM sentence", s["server_first_sentence_s"]),
        stat_row("Server: STT start to first audio chunk queued", s["server_first_audio_s"]),
        stat_row("Server: STT start to response complete", s["server_total_s"]),
        stat_row("Server: TTS synthesis time per turn", s["tts_synth_s"]),
        stat_row("Server: TTS synthesis time per sentence", s["tts_per_sentence_s"]),
        "",
    ]

    ok_rows = [r for r in warm["rows"] if r["status"] == "ok"]
    derived = []
    for r in ok_rows:
        if r.get("e2e_first_audio_s") is not None and r.get("server_first_audio_s") is not None:
            derived.append(
                r["e2e_first_audio_s"]
                - latency["environment"]["vad_min_silence_ms"] / 1000.0
                - r["server_first_audio_s"]
            )
    import numpy as np

    if derived:
        arr = np.asarray(derived)
        lines += [
            "Derived (not directly measured): client first-audio latency minus the VAD silence window minus the "
            "server's STT-start-to-first-audio time. This residual is transport, the Go hop, VAD frame alignment "
            "and scheduling.",
            "",
            "| Residual (seconds) | n | min | p50 | p95 | max |",
            "|---|---|---|---|---|---|",
            f"| e2e - silence window - server first audio | {arr.size} | {arr.min():.3f} | "
            f"{np.percentile(arr, 50):.3f} | {np.percentile(arr, 95):.3f} | {arr.max():.3f} |",
            "",
        ]

    gap = s["max_playback_gap_s"]
    lines += [
        "### Playback gaps inside a reply",
        "",
        "If a client plays the reply in real time from its first chunk, this is the longest time its audio buffer "
        "ran dry between two chunks of the same reply (an inter-sentence stall). It is the quantity the "
        "AetherRTC speaking-state gate below reacts to: the old goroutine reopened the mic after about 300 ms of "
        "it, the fixed one after about 400 ms.",
        "",
        "| Longest gap in a reply (seconds) | n | min | p50 | p95 | max |",
        "|---|---|---|---|---|---|",
        f"| per turn | {cell(gap, 'n', 1, 0)} | {cell(gap, 'min', 1, 3)} | {cell(gap, 'p50', 1, 3)} | {cell(gap, 'p95', 1, 3)} | {cell(gap, 'max', 1, 3)} |",
        "",
        f"Turns with a gap over 300 ms: {s['turns_with_gap_over_300ms']} of {gap.get('n', 0)}. "
        f"Turns with a gap over 400 ms: {s['turns_with_gap_over_400ms']} of {gap.get('n', 0)}.",
        "",
    ]

    rtf = s["tts_rtf_per_turn"]
    lines += [
        "### TTS real-time factor",
        "",
        "Audio seconds produced divided by wall seconds spent inside `Synthesizer.synthesize` (higher is faster; "
        "above 1.0 means faster than real time). Piper runs on the CPU here.",
        "",
        f"- Aggregate over all turns: {s['tts_rtf_aggregate']:.2f}" if s["tts_rtf_aggregate"] else "- Aggregate: not available",
        "",
        "| Per-turn RTF | n | min | p50 | p95 | max |",
        "|---|---|---|---|---|---|",
        f"| audio s / synth s | {cell(rtf, 'n', 1, 0)} | {cell(rtf, 'min', 1, 2)} | {cell(rtf, 'p50', 1, 2)} | {cell(rtf, 'p95', 1, 2)} | {cell(rtf, 'max', 1, 2)} |",
        "",
        "### STT accuracy on this audio",
        "",
        "Word error rate of the server's transcript against the known text (synthetic Piper speech through an 8 kHz "
        "G.711 round trip, one voice; this is easier than real callers).",
        "",
        "| WER | n | min | p50 | p95 | max | mean |",
        "|---|---|---|---|---|---|---|",
        f"| fraction | {cell(s['wer'], 'n', 1, 0)} | {cell(s['wer'], 'min', 1, 3)} | {cell(s['wer'], 'p50', 1, 3)} | {cell(s['wer'], 'p95', 1, 3)} | {cell(s['wer'], 'max', 1, 3)} | {cell(s['wer'], 'mean', 1, 3)} |",
        "",
    ]

    by_utt = collections.defaultdict(list)
    for r in ok_rows:
        by_utt[r["utterance_id"]].append(r)
    lines += [
        "### By utterance",
        "",
        "| Utterance | Speech (s) | n | Client first audio p50 (s) | Server first audio p50 (s) | Response audio p50 (s) |",
        "|---|---|---|---|---|---|",
    ]
    for utt in sorted(by_utt):
        rows = by_utt[utt]
        med = lambda key: float(np.median([r[key] for r in rows if r.get(key) is not None]))
        lines.append(
            f"| {utt} | {rows[0]['speech_s']:.2f} | {len(rows)} | {med('e2e_first_audio_s'):.3f} | "
            f"{med('server_first_audio_s'):.3f} | {med('response_audio_s'):.2f} |"
        )
    lines.append("")

    gpu = warm["gpu"]
    lines += [
        "### GPU memory",
        "",
        f"Sampled with `nvidia-smi` every 200 ms ({gpu.get('samples', 0)} samples), starting before the stack "
        f"booted and after the model was unloaded (unloaded: {gpu.get('model_unloaded_before_start')}). "
        f"First sample: {gpu.get('mem_used_mib_first', '-')} MiB used. "
        f"Peak during the run: {gpu.get('mem_used_mib_peak', '-')} MiB of 4096. "
        f"Peak GPU utilisation {gpu.get('gpu_util_pct_peak', '-')}%, peak power {gpu.get('power_w_peak', '-')} W.",
        "",
        "Only the LLM is on the GPU; Whisper (int8), Piper and Silero all run on the CPU.",
        "",
        "```",
        warm["ollama_ps_after_warmup"],
        "```",
        "",
    ]

    errors = group_errors(warm["server_error_lines"])
    if errors:
        sessions = params.get("warm_runs", 1) * (params["passes"] + 1)
        lines += [
            "### Error lines in the inference log during the warm runs",
            "",
            f"The runs opened {sessions} sessions (one warm-up plus {params['passes']} calls per run). One "
            "`gRPC stream error in read pump` with a traceback is logged per session when it ends, because the "
            "orchestrator cancels the agent stream on hang-up and `_read_pump` logs the resulting `RpcError` at "
            "ERROR level. A count above the number of sessions would be a real fault. Changed after this run: "
            "a hang-up now logs `Client disconnected` at INFO.",
            "",
        ]
        for message, count in errors.most_common():
            lines.append(f"- {count} x `{message}`")
        lines.append("")
    return lines


def vad_section(vad):
    if not vad:
        return []
    conditions = vad["conditions"]
    carried = conditions["carried_state"]
    fresh = conditions["fresh_state_per_utterance"]
    reset = conditions.get("reset_at_boundary")
    tol2 = conditions.get("carried_state_dip_tolerance_2")
    tol3 = conditions.get("carried_state_dip_tolerance_3")
    short_id = min(vad["utterances"], key=lambda u: vad["utterances"][u]["speech_s"])
    lines = [
        "## VAD detection of short utterances",
        "",
        "Found because one warm-run turn (the 0.48 s \"Hello.\") produced no reply: the server log shows no speech "
        "start for it, so the audio never reached STT. This experiment replays the same audio through the real "
        "`AudioPreprocessor` and `VADDetector` (no models other than Silero), "
        f"with min silence {vad['min_silence_ms']} ms as set in `main.py`, {vad['passes']} shuffled calls of "
        "12 utterances each, 3.0 s of silence after each utterance, in these conditions:",
        "",
        "- **carried state**: one detector for the whole call, as the pipeline does.",
        "- **fresh state**: a new detector for every utterance. An experiment, not how the service runs. It is "
        "deterministic (same audio, same 300 ms preroll every trial), so 50/50 here only shows the audio is "
        "detectable with that preroll.",
        "- **reset at boundary**: `VADDetector.reset()` called when each utterance ends, which is the obvious fix.",
        "- **dip tolerance 2 / 3**: carried state, but the 8-frame confirmation tolerates up to 2 or 3 consecutive "
        "sub-threshold frames (`max_dip_frames`, off by default).",
        "",
        "| Utterance | Speech (s) | Carried | Fresh | Reset at boundary | Dip tol. 2 | Dip tol. 3 |",
        "|---|---|---|---|---|---|---|",
    ]

    def cell_for(cond, utt):
        if not cond:
            return "-"
        s = cond["per_utterance"][utt]
        return f"{s['detected']}/{s['trials']}"

    for utt, info in vad["utterances"].items():
        lines.append(
            f"| {utt}: {info['text'][:42]}{'...' if len(info['text']) > 42 else ''} | {info['speech_s']:.2f} | "
            f"{cell_for(carried, utt)} | {cell_for(fresh, utt)} | {cell_for(reset, utt)} | "
            f"{cell_for(tol2, utt)} | {cell_for(tol3, utt)} |"
        )
    positions = sorted(m["position"] for m in carried["misses"])
    lines += [
        "",
        f"Misses with carried state: {len(carried['misses'])}, at positions in the call: {positions}.",
        "",
    ]

    sweep = vad.get("preroll_sweep")
    if sweep:
        prerolls = list(next(iter(sweep.values())).keys())
        lines += [
            "### Detection depends on the silence before the word",
            "",
            "A fresh detector, digital silence of the given length before the utterance, one trial per cell "
            "(deterministic). `yes` means speech start fired.",
            "",
            "| Utterance | " + " | ".join(f"{p} s" for p in prerolls) + " |",
            "|---|" + "---|" * len(prerolls),
        ]
        for utt, row in sweep.items():
            lines.append(
                f"| {utt} | " + " | ".join("yes" if row[p]["detected"] else "**no**" for p in prerolls) + " |"
            )
        lines += [
            "",
            f"Silero scores for the first {len(sweep[short_id][prerolls[0]]['onset_scores'])} frames "
            f"(32 ms each) of {short_id}, from speech onset:",
            "",
        ]
        for p in prerolls:
            cell_row = sweep[short_id][p]
            lines.append(
                f"- preroll {p} s, {'detected' if cell_row['detected'] else 'missed'}: {cell_row['onset_scores']}"
            )
        lines.append("")

    triggers = vad.get("false_triggers")
    if triggers:
        names = list(next(iter(triggers.values())).keys())
        lines += [
            "### False triggers on synthetic non-speech",
            "",
            "5 s of each stimulus, 10 seeds, count of trials where speech start fired. White noise at three levels, "
            "clicks, 1 kHz tone bursts, 4 Hz modulated noise, 120 Hz hum. This is a weak negative control: no "
            "keyboard, television, other voices or room noise.",
            "",
            "| Setting | " + " | ".join(names) + " |",
            "|---|" + "---|" * len(names),
        ]
        for setting, stimuli in triggers.items():
            lines.append(
                f"| {setting} | " + " | ".join(f"{stimuli[n]['triggered']}/{stimuli[n]['trials']}" for n in names) + " |"
            )
        lines.append("")

    lines += [
        "Reading: speech must score above the 0.5 threshold on 8 consecutive frames (about 250 ms) before an "
        "utterance starts, and one sub-threshold frame in that window resets the count "
        "(`services/inference-py/vad/detector.py`, `_handle_speech_starting`). A one-word utterance is about 15 "
        "frames long, so a hesitation of one or two frames in the first part of the word can leave the longest run "
        "one frame short. Resetting the detector at the utterance boundary does not remove the misses, so stale "
        "recurrent state is not the cause. Detection of the same audio varies with how much silence precedes it, "
        "which points at the model's response to the word onset after silence together with the strict "
        "consecutive-frame rule. Tolerating short dips recovers only part of the misses. Not adopted in `main.py`: "
        "tuning on a single 0.48 s utterance would be overfitting, and a proper fix needs more short utterances "
        "and real-noise negatives. Synthetic single-voice speech; real callers will differ.",
        "",
    ]
    return lines


def cold_section(latency):
    cold = latency.get("cold")
    if not cold:
        return []
    params = latency["parameters"]
    ready = cold["inference_ready_s"]
    lines = [
        "## Cold start",
        "",
        f"{params['cold_repeats']} repeats. Before each: both services killed, `ollama stop` run so the LLM is "
        "unloaded from VRAM "
        f"(confirmed unloaded: {cold['model_unloaded_before_each_repeat']}), then fresh processes and a new call "
        "with 3 turns. The OS file cache is not flushed, so model files are likely already in RAM; a true "
        "first boot after a reboot would be slower than this.",
        "",
        f"Inference process spawn to listening: min {cell(ready, 'min', 1, 2)} s, "
        f"p50 {cell(ready, 'p50', 1, 2)} s, max {cell(ready, 'max', 1, 2)} s. This includes a network call to "
        "huggingface.co made by faster-whisper at startup.",
        "",
        "With 5 samples per row, p95 and p99 are interpolated near the maximum and should not be quoted as tail estimates.",
        "",
        "| Turn in call | n | client first audio min / p50 / max (s) | server STT p50 (s) | server TTFT p50 (s) | TTS synth p50 (s) | server first audio p50 (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for idx, s in sorted(cold["summary_by_turn"].items()):
        e2e = s["e2e_first_audio_s"]
        lines.append(
            f"| {idx} | {cell(e2e, 'n', 1, 0)} | {cell(e2e, 'min', 1, 2)} / {cell(e2e, 'p50', 1, 2)} / {cell(e2e, 'max', 1, 2)} "
            f"| {cell(s['server_stt_s'], 'p50', 1, 2)} | {cell(s['server_ttft_s'], 'p50', 1, 2)} "
            f"| {cell(s['tts_synth_s'], 'p50', 1, 2)} | {cell(s['server_first_audio_s'], 'p50', 1, 2)} |"
        )
    lines.append("")
    statuses = collections.Counter(r["status"] for r in cold["rows"])
    lines += [f"Turn outcomes: {dict(statuses)}.", ""]
    gpu = cold["gpu"]
    lines += [
        f"VRAM across the cold repeats: first sample {gpu.get('mem_used_mib_first', '-')} MiB, "
        f"peak {gpu.get('mem_used_mib_peak', '-')} MiB.",
        "",
    ]
    return lines


def aether_section(aether):
    if not aether:
        return []
    lines = [
        "## AetherRTC fix 632fa47: before and after",
        "",
        f"Before: `{aether['before_commit'][:7]}` (first parent of merge `bf008f1`). After: `{aether['after_commit'][:7]}`. "
        "Both built from separate git worktrees, so the AetherRTC working tree was never touched.",
        "",
    ]
    opening = aether.get("bridge_open")
    if opening:
        dns = aether["dns"]
        lines += [
            "### Bridge construction to first stream open",
            "",
            "Timer starts before `bridge.NewClient` and stops when a stub gateway server in the same process "
            "receives the first message (`START_SESSION`) on the stream. Target is `localhost:<port>`, as in "
            "`cmd/gateway/main.go`. Runs are interleaved across arms. The control arm is my addition: the fixed "
            "code with the `passthrough:///` prefix removed, which separates the resolver change from the "
            "`conn.Connect()` change.",
            "",
            "| Arm | n | min (ms) | p50 (ms) | p95 (ms) | max (ms) | errors |",
            "|---|---|---|---|---|---|---|",
        ]
        for arm, label in (
            ("before", "before fix"),
            ("after", "after fix"),
            ("after_dns_control", "after fix, dns resolver (control)"),
        ):
            t = opening[arm]["server_received_ms"]
            lines.append(
                f"| {label} | {cell(t, 'n', 1, 0)} | {cell(t, 'min', 1, 1)} | {cell(t, 'p50', 1, 1)} | "
                f"{cell(t, 'p95', 1, 1)} | {cell(t, 'max', 1, 1)} | {len(opening[arm]['errors'])} |"
            )
        lines += [
            "",
            f"Claim: about 11.1 s before and about 5 ms after. "
            f"This host today: a TXT lookup for `_grpc_config.localhost` took "
            f"{dns['txt_lookup_seconds']:.2f} s and ended with `{dns['txt_lookup_outcome']}`. "
            f"DNS servers: {'; '.join(dns['dns_servers']) or 'none listed'}. "
            "The stall comes from this host's DNS behaviour, so a result near zero for the before arm means it "
            "does not reproduce here today.",
            "",
        ]

    gate = aether.get("speaking_gate")
    if gate:
        lines += [
            "### Agent-speaking state across an inter-sentence gap",
            "",
            "The real `NewPeerSession` playback goroutine is driven in-process (the outbound track is not bound to "
            "a peer, so `WriteSample` is a no-op). Sentence 1 is 1.0 s of audio delivered in 4096-byte chunks like "
            "the Python service does. Sentence 2 is delivered a chosen gap after sentence 1 finishes playing. "
            "`AgentSpeaking` is sampled every millisecond. \"Unmuted mid-reply\" means the flag went false before "
            "sentence 2 arrived, which is when the mic would be open while the agent is still mid-answer. "
            "\"Mic open\" is how long it stayed open before sentence 2 started. Sentence 2 is also 1.0 s of audio, "
            "so a reopen time near 1400 ms means the gate released about 400 ms after the reply's audio ended; "
            "the fixed goroutine catches up after short gaps by emitting up to 5 frames per tick, which "
            "shortens that figure.",
            "",
            "| Gap (ms) | before: unmuted / reps | before: mic open (ms) | before: mic reopens after sentence 2 delivered (ms) | after: unmuted / reps | after: mic open (ms) | after: mic reopens after sentence 2 delivered (ms) |",
            "|---|---|---|---|---|---|---|",
        ]
        before, after = gate.get("before", {}), gate.get("after", {})
        if "by_gap" in before and "by_gap" in after:
            for gap in sorted(before["by_gap"], key=int):
                b, a = before["by_gap"][gap], after["by_gap"][gap]
                lines.append(
                    f"| {gap} | {b['unmuted_mid_reply']}/{b['reps']} | {cell(b['mid_reply_open_ms'], 'p50', 1, 0)} | "
                    f"{cell(b['release_after_delivery_ms'], 'p50', 1, 0)} | "
                    f"{a['unmuted_mid_reply']}/{a['reps']} | {cell(a['mid_reply_open_ms'], 'p50', 1, 0)} | "
                    f"{cell(a['release_after_delivery_ms'], 'p50', 1, 0)} |"
                )
        else:
            lines.append(f"| error | {before.get('error', '')[:200]} | | | {after.get('error', '')[:200]} | | |")
        lines.append("")
    return lines


def claims_section(latency, aether):
    lines = ["## Earlier claims against this data", "", "| Claim | Source | Measured | Verdict |", "|---|---|---|---|"]
    warm = latency.get("warm") if latency else None
    if warm:
        s = warm["summary"]
        ttft = scalar(s["server_ttft_s"], "p50")
        first_audio = scalar(s["server_first_audio_s"], "p50")
        stt = scalar(s["server_stt_s"], "p50")
        e2e = scalar(s["e2e_first_audio_s"], "p50")
        e2e_minus = None if e2e is None else e2e - latency["environment"]["vad_min_silence_ms"] / 1000.0
        lines += [
            f"| LLM TTFT 0.43 to 0.50 s | docs/backlog.md | p50 {ttft:.3f} s | {verdict(ttft, 0.43, 0.50)} |" if ttft else "| LLM TTFT | docs/backlog.md | n/a | not measured |",
            f"| STT start to first audio byte 1.0 to 1.1 s | docs/backlog.md | p50 {first_audio:.3f} s (server side) | {verdict(first_audio, 1.0, 1.1)} |" if first_audio else "| STT to first audio | docs/backlog.md | n/a | not measured |",
            f"| STT 0.70 to 0.82 s | earlier notes | p50 {stt:.3f} s | {verdict(stt, 0.70, 0.82)} |" if stt else "| STT | earlier notes | n/a | not measured |",
            f"| Perceived delay 1.8 to 2.0 s after the user stops | earlier notes | p50 {e2e:.3f} s including the {latency['environment']['vad_min_silence_ms']} ms silence window ({e2e_minus:.3f} s after it) | {verdict(e2e, 1.8, 2.0)} |" if e2e else "| Perceived delay | earlier notes | n/a | not measured |",
        ]
        rtf = s["tts_rtf_aggregate"]
        synth = scalar(s["tts_per_sentence_s"], "p50")
        if synth:
            lines.append(f"| Piper warm 0.06 to 0.18 s per sentence | earlier notes | p50 {synth:.3f} s per sentence | {verdict(synth, 0.06, 0.18)} |")
    cold = latency.get("cold") if latency else None
    if cold:
        first = cold["summary_by_turn"].get("1", {}).get("tts_synth_s")
        v = scalar(first, "p50")
        if v:
            lines.append(f"| Piper cold 2.1 to 5.0 s on the first synthesis | earlier notes | first-turn p50 {v:.3f} s | {verdict(v, 2.1, 5.0)} |")
    if aether and aether.get("bridge_open"):
        before = scalar(aether["bridge_open"]["before"]["server_received_ms"], "p50")
        after = scalar(aether["bridge_open"]["after"]["server_received_ms"], "p50")
        if before is not None and after is not None:
            lines.append(
                f"| gRPC stream setup about 11.1 s before, about 5 ms after | commit 632fa47 | p50 {before / 1000:.3f} s before, {after:.1f} ms after | "
                f"{'reproduces' if before > 5000 else 'does not reproduce on this host today'} |"
            )
    lines.append("")
    return lines


def limitations_section(latency):
    lines = [
        "## What this does not measure",
        "",
        "- Browser capture, WebRTC jitter buffers and playout. The client here is a gRPC client of the orchestrator, "
        "so AetherRTC's media path is not in the end-to-end numbers. Its two fixes are measured separately above.",
        "- Real speech. Utterances are Piper-synthesised, one voice, passed through G.711 8 kHz. Whisper and VAD "
        "will find this easier than a noisy phone line, so STT time and WER are optimistic.",
        "- Digital silence between utterances; no background noise, so VAD false positives are not exercised.",
        "- Playback time. The next turn starts about 1 s after the previous response finished arriving, not after it "
        "would have finished playing.",
        "- A cross-process clock. The residual in the derived table absorbs everything between the VAD boundary "
        "and the client, and cannot be split further without synchronised clocks.",
        "- Concurrency. One call at a time; there is no multi-session load test.",
        "- A true first boot: the OS file cache is warm in the cold run.",
        "- p99 with small n. A p99 over about 60 turns is within one or two samples of the maximum.",
        "",
    ]
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    latency = load(os.path.join(args.data, "latency.json"))
    aether = load(os.path.join(args.data, "aether.json"))
    vad = load(os.path.join(args.data, "vad_detection.json"))
    if not latency and not aether and not vad:
        print("no benchmark data found", file=sys.stderr)
        return 1

    lines = ["# Benchmarks", "", "Generated by `scripts/bench/make_report.py` from `docs/benchmarks/latest/`. Do not edit by hand.", ""]
    lines += [
        "## Reproduce",
        "",
        "Stop any running stack first (ports 50051 and 50052 must be free). From the repository root:",
        "",
        "```",
        "services/inference-py/.venv/Scripts/python.exe scripts/bench/make_utterances.py   # only to regenerate the audio",
        "services/inference-py/.venv/Scripts/python.exe scripts/bench/bench_latency.py all        # cold start + 3 warm runs, about 45 minutes",
        "services/inference-py/.venv/Scripts/python.exe scripts/bench/bench_vad_detection.py     # about 2 minutes",
        "services/inference-py/.venv/Scripts/python.exe scripts/bench/bench_aether.py            # about 5 minutes",
        "```",
        "",
        "Each script rewrites this file from the raw data in `docs/benchmarks/latest/`. Ollama must be running with "
        "`qwen2.5:3b` pulled. `bench_latency.py` also accepts `warm`, `cold`, `--warm-runs N`, `--passes N` and "
        "`--cold-repeats N`. Do not use the machine for other work while it runs; CPU contention changes the numbers.",
        "",
    ]
    if latency:
        lines += environment_section(latency["environment"])
        lines += [
            "## How each number is measured",
            "",
            "The driver (`scripts/bench/bench_latency.py`) opens a gRPC stream to the orchestrator gateway on `:50052`, "
            "the same interface AetherRTC uses, and sends 8 kHz 16-bit PCM in 20 ms frames paced in real time. "
            "Both services are started by the script with logs captured.",
            "",
            "- **End of speech**: the instant the last speech frame is handed to gRPC. Each recording is trimmed so "
            "it ends within 20 ms of the last sample above about -40 dBFS, so this is the real end of speech and not "
            "the synthesiser's trailing silence. 300 ms of silence precedes each utterance and silence frames "
            "continue after it until the first reply byte arrives.",
            f"- **Client first audio**: the instant the first reply `AudioChunk` is received, minus end of speech. This "
            f"includes the {latency['environment']['vad_min_silence_ms']} ms of silence the VAD needs to close the utterance.",
            "- **Server timers** come from one `BENCH` log line per utterance added to `_run_utterance` in "
            "`services/inference-py/main.py` (`time.perf_counter`, starting when the worker begins the utterance). "
            "STT is the whole `transcribe()` call including its temp-file write. TTFT is read from the existing "
            "`TTFT:` log line. TTS time wraps only `synthesize()`.",
            "- **Mic gate** is emulated: sending stops when the reply starts, as AetherRTC drops mic frames while "
            "the agent speaks, and resumes after the reply has finished arriving.",
            "",
        ]
        lines += warm_section(latency)
        lines += cold_section(latency)
    lines += vad_section(vad)
    lines += aether_section(aether)
    lines += claims_section(latency, aether)
    lines += limitations_section(latency)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
