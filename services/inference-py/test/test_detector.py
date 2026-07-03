"""Milestone 2 acceptance test for VADDetector.

Builds a test sequence from real recorded silence and real recorded speech
sliced out of an existing WAV file, rather than synthetic zero/noise frames.
Silero VAD is a trained model -- synthetic signals do not reliably produce
predictable speech/silence scores, so this test would be meaningless built
any other way.

Sequence under test: 20 silence frames, 30 speech frames, 5 silence frames
(the mid-sentence pause), 30 speech frames, 20 silence frames.

Expected result: exactly one START_SPEECH and exactly one END_OF_UTTERANCE.
The 5-frame dip (160ms) must not trigger an end, since it is well under the
500ms min_silence_duration_ms default. The final 20-frame silence (640ms)
must trigger one.
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

MODEL_PATH = os.path.abspath(
    os.path.join(SERVICE_DIR, "models", "silero_vad.onnx")
)
SOURCE_WAV = os.path.abspath(
    os.path.join(REPO_ROOT, "test_data", "input.wav")
)
TARGET_SR = 16000
FRAME_SIZE = 512

# Regions identified from the earlier VAD confidence timeline on input_1.wav.
# Silence: 0.000s-0.480s (score < 0.5 throughout).
# Speech: 1.280s-1.600s (score consistently > 0.85, the cleanest stretch).
SILENCE_REGION_SEC = (0.0, 0.48)
SPEECH_REGION_SEC = (1.28, 1.60)


def load_and_resample(path: str) -> np.ndarray:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Source WAV not found: {path}")

    with wave.open(path, "rb") as wf:
        original_sr = wf.getframerate()
        raw_bytes = wf.readframes(wf.getnframes())

    audio_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
    audio_float = audio_int16.astype(np.float32) / 32768.0

    if original_sr == TARGET_SR:
        return audio_float

    gcd = np.gcd(TARGET_SR, original_sr)
    up = TARGET_SR // gcd
    down = original_sr // gcd
    return scipy.signal.resample_poly(audio_float, up, down).astype(np.float32)


def slice_frames(audio: np.ndarray, start_sec: float, end_sec: float) -> list:
    start_sample = int(start_sec * TARGET_SR)
    end_sample = int(end_sec * TARGET_SR)
    region = audio[start_sample:end_sample]

    num_frames = len(region) // FRAME_SIZE
    return [
        region[i * FRAME_SIZE : (i + 1) * FRAME_SIZE] for i in range(num_frames)
    ]


def tile_to_count(frames: list, count: int) -> list:
    if not frames:
        raise ValueError("Cannot tile from an empty frame list.")
    tiled = (frames * (count // len(frames) + 1))[:count]
    return tiled


def main():
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Silero model file not found: {MODEL_PATH}")

    audio = load_and_resample(SOURCE_WAV)

    silence_frames = slice_frames(audio, *SILENCE_REGION_SEC)
    speech_frames = slice_frames(audio, *SPEECH_REGION_SEC)

    print(f"Extracted {len(silence_frames)} real silence frames.")
    print(f"Extracted {len(speech_frames)} real speech frames.")

    sequence = (
        tile_to_count(silence_frames, 20)
        + tile_to_count(speech_frames, 30)
        + tile_to_count(silence_frames, 5)
        + tile_to_count(speech_frames, 30)
        + tile_to_count(silence_frames, 20)
    )

    detector = VADDetector(MODEL_PATH)

    start_speech_count = 0
    end_utterance_count = 0

    print("\nFrame  State-before   Command")
    print("-" * 40)
    for i, frame in enumerate(sequence):
        state_before = detector._vad_state.value
        command = detector.process_frames(frame)

        if command != VADCommand.NONE:
            print(f"{i:5d}  {state_before:14s} {command.value}")

        if command == VADCommand.START_SPEECH:
            start_speech_count += 1
        if command == VADCommand.END_OF_UTTERANCE:
            end_utterance_count += 1

    print("-" * 40)
    print(f"\nSTART_SPEECH fired: {start_speech_count} (expected 1)")
    print(f"END_OF_UTTERANCE fired: {end_utterance_count} (expected 1)")

    if start_speech_count == 1 and end_utterance_count == 1:
        print("\nPASS")
    else:
        print("\nFAIL")


if __name__ == "__main__":
    main()