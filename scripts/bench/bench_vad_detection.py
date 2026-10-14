import argparse
import json
import os
import random
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import bench_latency as common

INFERENCE_DIR = common.INFERENCE_DIR
sys.path.insert(0, INFERENCE_DIR)
from vad import AudioPreprocessor, VADCommand, VADDetector

MODEL_PATH = os.path.join(INFERENCE_DIR, "models", "silero_vad.onnx")


def feed(vad, preprocessor, frames, reset_on_end=False):
    starts = ends = 0
    for packet in frames:
        for frame in preprocessor.push(packet):
            command = vad.process_frames(frame)
            if command == VADCommand.START_SPEECH:
                starts += 1
            elif command == VADCommand.END_OF_UTTERANCE:
                ends += 1
                if reset_on_end:
                    vad.get_utterance_frames()
                    vad.reset()
    return starts, ends


def run_condition(
    fresh_per_utterance, args, min_silence, utterances, ids, reset_on_end=False, max_dip=0
):
    silence = [common.SILENCE_FRAME] * int(args.silence_s / common.FRAME_S)
    preroll = [common.SILENCE_FRAME] * common.PREROLL_FRAMES

    per_utt = {u: {"trials": 0, "detected": 0, "ended": 0, "split": 0} for u in ids}
    misses = []
    for p in range(1, args.passes + 1):
        order = ids[:]
        random.Random(args.seed + p).shuffle(order)
        vad = VADDetector(MODEL_PATH, min_silence_duration=min_silence, max_dip_frames=max_dip)
        preprocessor = AudioPreprocessor(source_sr=common.SAMPLE_RATE)
        previous = None
        for position, utt in enumerate(order, 1):
            if fresh_per_utterance:
                vad = VADDetector(MODEL_PATH, min_silence_duration=min_silence, max_dip_frames=max_dip)
                preprocessor = AudioPreprocessor(source_sr=common.SAMPLE_RATE)
            frames = preroll + common.to_frames(utterances[utt]["pcm"]) + silence
            starts, ends = feed(vad, preprocessor, frames, reset_on_end)
            stats = per_utt[utt]
            stats["trials"] += 1
            stats["detected"] += 1 if starts >= 1 else 0
            stats["ended"] += 1 if ends >= 1 else 0
            stats["split"] += 1 if starts > 1 else 0
            if starts == 0:
                misses.append(
                    {"pass": p, "position": position, "utterance": utt, "previous": previous}
                )
            previous = utt
    return per_utt, misses


def synthetic_non_speech(seed):
    rng = np.random.default_rng(seed)
    n = common.SAMPLE_RATE * 5
    t = np.arange(n) / common.SAMPLE_RATE

    def pcm(x):
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()

    stimuli = {}
    for amp in (500, 2000, 6000):
        stimuli[f"white_noise_{amp}"] = pcm(rng.normal(0, amp, n))
    clicks = np.zeros(n)
    clicks[:: int(0.4 * common.SAMPLE_RATE)] = 16000
    stimuli["clicks_every_400ms"] = pcm(clicks)
    stimuli["tone_bursts_1khz"] = pcm(
        6000 * np.sin(2 * np.pi * 1000 * t) * ((t % 0.3) < 0.15)
    )
    stimuli["modulated_noise_4hz"] = pcm(
        rng.normal(0, 4000, n) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t))
    )
    stimuli["hum_120hz"] = pcm(3000 * np.sin(2 * np.pi * 120 * t))
    return stimuli


def run_false_triggers(args, min_silence, seeds=10):
    result = {}
    for max_dip in (0, 2, 3):
        per_stimulus = {}
        for seed in range(seeds):
            for name, pcm in synthetic_non_speech(args.seed + seed).items():
                vad = VADDetector(
                    MODEL_PATH, min_silence_duration=min_silence, max_dip_frames=max_dip
                )
                preprocessor = AudioPreprocessor(source_sr=common.SAMPLE_RATE)
                starts, _ = feed(vad, preprocessor, common.to_frames(pcm))
                stats = per_stimulus.setdefault(name, {"trials": 0, "triggered": 0})
                stats["trials"] += 1
                stats["triggered"] += 1 if starts >= 1 else 0
        result[f"max_dip_frames_{max_dip}"] = per_stimulus
    return result


PREROLL_SWEEP_S = (0.3, 0.8, 1.5, 2.2, 3.0, 6.0)
ONSET_FRAMES = 14


def run_preroll_sweep(utterances, ids, min_silence):
    sweep = {}
    for utt in ids:
        row = {}
        for preroll_s in PREROLL_SWEEP_S:
            vad = VADDetector(MODEL_PATH, min_silence_duration=min_silence)
            preprocessor = AudioPreprocessor(source_sr=common.SAMPLE_RATE)
            scores = []
            infer = vad._infer

            def spy(frame, infer=infer, scores=scores):
                score, state = infer(frame)
                scores.append(score)
                return score, state

            vad._infer = spy
            frames = (
                [common.SILENCE_FRAME] * int(preroll_s / common.FRAME_S)
                + common.to_frames(utterances[utt]["pcm"])
                + [common.SILENCE_FRAME] * int(3.0 / common.FRAME_S)
            )
            starts, _ = feed(vad, preprocessor, frames)
            onset = int(preroll_s * 1000 / VADDetector.FRAME_DURATION_MS)
            row[str(preroll_s)] = {
                "detected": starts >= 1,
                "onset_scores": [round(s, 2) for s in scores[onset : onset + ONSET_FRAMES]],
            }
        sweep[utt] = row
    return sweep


def main():
    parser = argparse.ArgumentParser(
        description="Per-utterance VAD detection rate through the real preprocessor and detector"
    )
    parser.add_argument("--passes", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--min-silence-ms", type=int, default=0)
    parser.add_argument("--silence-s", type=float, default=3.0)
    parser.add_argument("--out", default=common.RESULT_DIR)
    args = parser.parse_args()

    min_silence = args.min_silence_ms or common.vad_min_silence_ms()
    utterances = common.load_utterances()
    ids = sorted(utterances)

    conditions = {}
    for name, fresh, reset_on_end, max_dip in (
        ("carried_state", False, False, 0),
        ("fresh_state_per_utterance", True, False, 0),
        ("reset_at_boundary", False, True, 0),
        ("carried_state_dip_tolerance_2", False, False, 2),
        ("carried_state_dip_tolerance_3", False, False, 3),
    ):
        per_utt, misses = run_condition(
            fresh, args, min_silence, utterances, ids, reset_on_end, max_dip
        )
        conditions[name] = {"per_utterance": per_utt, "misses": misses}

    summary = {
        "min_silence_ms": min_silence,
        "passes": args.passes,
        "silence_after_s": args.silence_s,
        "utterances": {
            u: {
                "text": utterances[u]["text"],
                "speech_s": len(utterances[u]["pcm"]) / 2 / common.SAMPLE_RATE,
            }
            for u in ids
        },
        "conditions": conditions,
        "false_triggers": run_false_triggers(args, min_silence),
        "preroll_sweep": run_preroll_sweep(utterances, ids, min_silence),
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "vad_detection.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    for name, data in conditions.items():
        print(f"== {name} ==")
        for u in ids:
            s = data["per_utterance"][u]
            print(
                f"{u} {summary['utterances'][u]['speech_s']:5.2f}s detected "
                f"{s['detected']:3d}/{s['trials']} ended {s['ended']:3d}/{s['trials']} split {s['split']}"
            )
    print("== false triggers on synthetic non-speech (trials triggered / trials) ==")
    for setting, stimuli in summary["false_triggers"].items():
        row = ", ".join(f"{n}={s['triggered']}/{s['trials']}" for n, s in stimuli.items())
        print(f"{setting}: {row}")
    print(f"wrote {path}")

    report = os.path.join(os.path.dirname(__file__), "make_report.py")
    command = [sys.executable, report, "--data", args.out]
    if os.path.abspath(args.out) != os.path.abspath(common.RESULT_DIR):
        command += ["--out", os.path.join(args.out, "benchmarks.md")]
    subprocess.run(command, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
