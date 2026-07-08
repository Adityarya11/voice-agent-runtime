"""Confirms RNN state genuinely carries context across an utterance boundary,
by comparing model output on the identical frame under two different prior
states rather than merely checking the state isn't all-zeros."""

import os
import sys
import wave

import numpy as np
import scipy.signal

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from vad import VADCommand, VADDetector

VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx")
SOURCE_WAV = os.path.join(os.path.dirname(__file__), "..", "..", "..", "test_data", "input_1.wav")
TARGET_SR = 16000
FRAME_SIZE = 512

SILENCE_REGION_SEC = (0.0, 0.48)
SPEECH_REGION_SEC = (1.28, 1.60)


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


def slice_frames(audio: np.ndarray, start_sec: float, end_sec: float) -> list:
    region = audio[int(start_sec * TARGET_SR):int(end_sec * TARGET_SR)]
    num_frames = len(region) // FRAME_SIZE
    return [region[i * FRAME_SIZE:(i + 1) * FRAME_SIZE] for i in range(num_frames)]


def test_rnn_state_persistence():
    audio = load_and_resample(SOURCE_WAV)
    silence_frames = slice_frames(audio, *SILENCE_REGION_SEC)
    speech_frames = slice_frames(audio, *SPEECH_REGION_SEC)

    utterance_1 = speech_frames + silence_frames * 3
    utterance_2_first_frame = speech_frames[0]

    primary = VADDetector(VAD_MODEL_PATH, min_speech_duration=64, min_silence_duration=64)

    start_count = 0
    end_count = 0
    state_after_utterance_1 = None

    for frame in utterance_1:
        command = primary.process_frames(frame)
        if command == VADCommand.START_SPEECH:
            start_count += 1
        elif command == VADCommand.END_OF_UTTERANCE:
            end_count += 1
            state_after_utterance_1 = primary._state.copy()

    print(f"[TEST PERSISTENCE] START_SPEECH: {start_count}, END_OF_UTTERANCE: {end_count}")
    assert start_count == 1, f"Expected one confirmed utterance start, got {start_count}"
    assert end_count == 1, f"Expected one utterance boundary, got {end_count}"
    assert state_after_utterance_1 is not None, "Boundary never captured a state snapshot."

    # Feed the identical first frame of utterance 2 into two independent detectors:
    # one carrying utterance 1's history forward, one starting fresh at zero state.
    # If the model produces different scores on the same frame, state is genuinely
    # influencing behavior across the boundary -- not just present, but load-bearing.
    primary._state = state_after_utterance_1
    score_with_history, _ = primary._infer(utterance_2_first_frame)

    fresh = VADDetector(VAD_MODEL_PATH)
    score_fresh, _ = fresh._infer(utterance_2_first_frame)

    print(f"[TEST PERSISTENCE] Score with carried context: {score_with_history:.4f}")
    print(f"[TEST PERSISTENCE] Score from zero-state detector: {score_fresh:.4f}")

    assert not np.isclose(score_with_history, score_fresh, atol=1e-4), (
        "VAD Context Leak! Identical frame produced identical score regardless "
        "of prior state -- state is not meaningfully influencing inference."
    )

    print("PASS: test_vad_state_persistence")


if __name__ == "__main__":
    test_rnn_state_persistence()