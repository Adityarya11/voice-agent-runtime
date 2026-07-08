"""confirm a short noise burst below `min_speech_duration` never crosses into confirmed speech."""

import os
import sys
import numpy as np
import onnxruntime as ort

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from vad import (
    VADCommand,
    VADDetector
)

VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx")

def test_false_start_gauntlet():
    # Initialize detector with default parameters: 250ms speech min, 500ms silence min
    detector = VADDetector(VAD_MODEL_PATH, min_speech_duration=250, min_silence_duration=500)
    
    # 1. Generate real high-scoring speech frames (e.g., score = 0.9)
    # 512 samples per frame. 4 frames = ~128ms, well under the 250ms window (~8 frames)
    speech_frame = np.ones(512, dtype=np.float32) * 0.5  # Simulated hot frame
    # We mock _infer to return a high score to guarantee we're testing the debounce engine,
    # avoiding acoustic edge-cases of short cuts
    original_infer = detector._infer
    
    # Force high score for first 4 frames, then near-zero silence score for subsequent frames
    scores_to_yield = [0.9, 0.85, 0.92, 0.88] + [0.01] * 30
    score_idx = 0
    
    def mock_infer(frame):
        nonlocal score_idx
        score = scores_to_yield[score_idx] if score_idx < len(scores_to_yield) else 0.01
        score_idx += 1
        # Maintain the internal RNN state signature
        _, hidden_state = original_infer(frame)
        return score, hidden_state

    detector._infer = mock_infer

    start_speech_count = 0
    end_utterance_count = 0

    # Feed the sequence through the state machine
    for _ in range(len(scores_to_yield)):
        dummy_frame = np.zeros(512, dtype=np.float32)
        command = detector.process_frames(dummy_frame)
        
        if command == VADCommand.START_SPEECH:
            start_speech_count += 1
        elif command == VADCommand.END_OF_UTTERANCE:
            end_utterance_count += 1

    print(f"[TEST FALSE START] Run Complete. Internal state reached: {detector._vad_state.value}")
    print(f"[TEST FALSE START] START_SPEECH fired: {start_speech_count}, END_OF_UTTERANCE fired: {end_utterance_count}")
    
    # Assertions
    assert start_speech_count == 0, "VAD bug: START_SPEECH fired for a transient noise below min_speech_duration!"
    assert end_utterance_count == 0, "VAD bug: END_OF_UTTERANCE fired without speech ever being confirmed!"
    assert detector._vad_state.value == "SILENCE", "VAD bug: Detector failed to cycle back to SILENCE state."
    print("✅ test_vad_false_start PASSED.")

if __name__ == "__main__":
    test_false_start_gauntlet()
    