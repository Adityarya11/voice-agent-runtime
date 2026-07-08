import os
import sys
import numpy as np

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from vad import VADDetector, VADCommand

VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx")

def test_rnn_state_persistence():
    # Build baseline fresh instance that remains untouched to map initial zero state structure
    baseline_detector = VADDetector(VAD_MODEL_PATH)
    initial_zero_state = baseline_detector._state.copy()
    
    # Primary evaluation unit
    primary_detector = VADDetector(VAD_MODEL_PATH, min_speech_duration=64, min_silence_duration=64)
    
    # Mocking real acoustic behavior sequentially:
    # Utterance 1 Speech (10 frames) -> Utterance 1 Silence (10 frames) -> 
    # Utterance 2 Speech (10 frames) -> Utterance 2 Silence (10 frames)
    scores = ([0.95] * 10) + ([0.02] * 10) + ([0.95] * 10) + ([0.02] * 10)
    score_idx = 0
    
    original_infer = primary_detector._infer
    def mock_infer(frame):
        nonlocal score_idx
        score = scores[score_idx]
        score_idx += 1
        # Execute real inference to let ONNX update the raw hidden math state variables
        _, hidden_state = original_infer(frame)
        return score, hidden_state

    primary_detector._infer = mock_infer

    start_speech_count = 0
    end_utterance_count = 0
    captured_mid_sequence_state = None

    for _ in range(len(scores)):
        dummy_frame = np.zeros(512, dtype=np.float32)
        command = primary_detector.process_frames(dummy_frame)
        
        if command == VADCommand.START_SPEECH:
            start_speech_count += 1
        elif command == VADCommand.END_OF_UTTERANCE:
            end_utterance_count += 1
            # Capture the exact mathematical RNN context block state right at the turn boundary
            if end_utterance_count == 1:
                captured_mid_sequence_state = primary_detector._state.copy()

    print(f"[TEST PERSISTENCE] Total START_SPEECH: {start_speech_count}, Total END_OF_UTTERANCE: {end_utterance_count}")
    
    # Assertions
    assert start_speech_count == 2, f"Expected exactly 2 conversational entries, got {start_speech_count}"
    assert end_utterance_count == 2, f"Expected exactly 2 turn completions, got {end_utterance_count}"
    assert captured_mid_sequence_state is not None, "Failed to capture turn boundary matrix state profile."
    
    # Prove that the mid-sequence state contains non-zero history tracking
    state_is_mutated = not np.array_equal(captured_mid_sequence_state, initial_zero_state)
    assert state_is_mutated, "VAD Context Leak! The RNN state array was wiped or reset back to zero at the turn boundary!"
    
    print("✅ test_vad_state_persistence PASSED.")

if __name__ == "__main__":
    test_rnn_state_persistence()