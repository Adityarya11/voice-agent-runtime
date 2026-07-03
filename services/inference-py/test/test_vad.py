"""Milestone 2 acceptance test for VADDetector.

Runs a single continuous recorded clip through the detector frame by frame,
in order, with no splicing or tiling. The clip must contain: a short speech
segment, a deliberate pause under 500ms, more speech, then over a second of
silence. This tests the debounce logic against real continuous audio instead
of manufactured sequences, which avoids introducing waveform discontinuities
that do not occur in natural speech.
"""

import os
import sys
import wave

import numpy as np
import scipy.signal

TEST_DIR = os.path.dirname(__file__)
SERVICE_DIR = os.path.abspath(os.path.join(TEST_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TEST_DIR, "..", "..", ".."))

if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

from vad.detector import VADCommand, VADDetector

MODEL_PATH = os.path.join(SERVICE_DIR, "models", "silero_vad.onnx")
TEST_WAV_CANDIDATES = [
    os.path.join(REPO_ROOT, "test_data", "vad_test.wav"),
    # os.path.join(REPO_ROOT, "test_data", "input.wav"),
]
TARGET_SR = 16000
FRAME_SIZE = 512


def resolve_test_wav() -> str:
    for path in TEST_WAV_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "No test WAV found. Tried: " + ", ".join(TEST_WAV_CANDIDATES)
    )


def load_and_resample(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        original_sr = wf.getframerate()
        raw_bytes = wf.readframes(wf.getnframes())

    audio_float = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    if original_sr == TARGET_SR:
        return audio_float

    gcd = np.gcd(TARGET_SR, original_sr)
    return scipy.signal.resample_poly(
        audio_float, TARGET_SR // gcd, original_sr // gcd
    ).astype(np.float32)


def main():
    audio = load_and_resample(resolve_test_wav())
    num_frames = len(audio) // FRAME_SIZE
    detector = VADDetector(MODEL_PATH)

    for i in range(num_frames):
        frame = audio[i * FRAME_SIZE:(i + 1) * FRAME_SIZE]
        command = detector.process_frames(frame)
        if command != VADCommand.NONE:
            timestamp_ms = i * VADDetector.FRAME_DURATION_MS
            print(f"frame {i:3d} ({timestamp_ms:6.0f}ms): {command.value}")


if __name__ == "__main__":
    main()