import os
import sys
import numpy as np

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from vad import VADDetector, VADCommand

VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx")

def test_ramble_max_ceiling():
    # Configure with a tight max_utterance_sec of 5 seconds to speed up testing
    # 5 seconds @ 16kHz with 512 frame sizes = exactly 156.25 -> 156 frames max ceiling
    max_sec = 5.0
    detector = VADDetector(VAD_MODEL_PATH, max_utterance_sec=max_sec)
    expected_max_frames = detector._max_utterance_frames

    # Mock infer to permanently simulate active human speech (score = 0.95)
    original_infer = detector._infer
    detector._infer = lambda frame: (0.95, detector._state)

    start_speech_idx = -1
    end_utterance_idx = -1
    start_speech_count = 0
    end_utterance_count = 0

    # Run for 200 continuous speech frames to comfortably overshoot the 156 ceiling
    for idx in range(200):
        dummy_frame = np.zeros(512, dtype=np.float32)
        command = detector.process_frames(dummy_frame)
        
        if command == VADCommand.START_SPEECH:
            start_speech_count += 1
            start_speech_idx = idx
        elif command == VADCommand.END_OF_UTTERANCE:
            end_utterance_count += 1
            end_utterance_idx = idx
            break  # Stop streaming once cutoff triggers

    print(f"[TEST RAMBLE] Expected ceiling frame index: {expected_max_frames}")
    print(f"[TEST RAMBLE] START_SPEECH fired at frame index: {start_speech_idx}")
    print(f"[TEST RAMBLE] END_OF_UTTERANCE forced at frame index: {end_utterance_idx}")

    # Assertions
    assert start_speech_count == 1, "START_SPEECH must fire exactly once upon crossing the entry threshold."
    assert end_utterance_count == 1, "Ceiling failed to trigger an END_OF_UTTERANCE command."
    
    # The absolute frame index where END_OF_UTTERANCE triggers must align with expected ceiling
    # because _utterance_frames appends frames every loop after entry validation.
    assert end_utterance_idx == expected_max_frames - 1, f"Ceiling mismatch! Cutoff fired at loop index {end_utterance_idx}, expected {expected_max_frames - 1}"
        
    print("✅ test_vad_ramble PASSED.")

if __name__ == "__main__":
    test_ramble_max_ceiling()