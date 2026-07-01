import numpy as np
import onnxruntime as ort
import wave
import scipy.signal
import os
import time

# ==============================================================================
# CONFIGURATION
# ==============================================================================
MODEL_PATH = os.path.join("vad", "models", "silero_vad.onnx")
TEST_WAV_PATH = "../../test_data/input.wav"
TARGET_SR = 16000
FRAME_SIZE = 512  # 32ms at 16kHz
THRESHOLD = 0.5   # Standard Silero threshold

# ==============================================================================
# LOAD MODEL
# ==============================================================================
print(f"Loading ONNX model from: {MODEL_PATH}")
session = ort.InferenceSession(MODEL_PATH)

# Initialize state (V6 format: single tensor, size 128)
state = np.zeros((2, 1, 128), dtype=np.float32)
sr = np.array(TARGET_SR, dtype=np.int64)

# ==============================================================================
# LOAD & PREPARE AUDIO
# ==============================================================================
print(f"Loading audio from: {TEST_WAV_PATH}")
with wave.open(TEST_WAV_PATH, 'rb') as wf:
    original_sr = wf.getframerate()
    raw_bytes = wf.readframes(wf.getnframes())

audio_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
audio_float = audio_int16.astype(np.float32) / 32768.0

if original_sr != TARGET_SR:
    num_samples = int(len(audio_float) * TARGET_SR / original_sr)
    audio_16k = scipy.signal.resample(audio_float, num_samples).astype(np.float32)
    print(f"Resampled {original_sr}Hz -> {TARGET_SR}Hz")
else:
    audio_16k = audio_float

total_frames = len(audio_16k) // FRAME_SIZE
print(f"Total audio duration: {len(audio_16k) / TARGET_SR:.2f} seconds")
print(f"Total frames to process: {total_frames} ({total_frames * 32}ms)\n")

# ==============================================================================
# RUN CONTINUOUS INFERENCE (Simulating _read_pump)
# ==============================================================================
print("Processing frames...")
start_time = time.time()

scores = []
for i in range(total_frames):
    # 1. Slice out exactly 512 samples
    start_idx = i * FRAME_SIZE
    end_idx = start_idx + FRAME_SIZE
    frame = audio_16k[start_idx:end_idx].reshape(1, FRAME_SIZE)
    
    # 2. Run ONNX inference, passing state forward
    outputs = session.run(None, {
        'input': frame,
        'sr': sr,
        'state': state
    })
    
    # 3. Extract score and UPDATE state (critical for RNN!)
    score = outputs[0][0][0]
    state = outputs[1]  # This is the new state for the next frame
    scores.append(score)

inference_time = time.time() - start_time
print(f"Inference complete in {inference_time:.3f}s "
      f"({(total_frames / inference_time):.1f} frames/sec)\n")

# ==============================================================================
# VISUALIZE THE OUTPUT
# ==============================================================================
print("=" * 60)
print("VAD CONFIDENCE TIMELINE")
print("=" * 60)
print("Each block represents ~160ms of audio.")
print(f"['#'] = Speech detected (>{THRESHOLD})")
print(f"['-'] = Silence/noise (<{THRESHOLD})")
print("-" * 60)

# Group frames into chunks of 5 (5 * 32ms = 160ms per block) for readability
chunk_size = 5
for i in range(0, len(scores), chunk_size):
    chunk = scores[i:i+chunk_size]
    avg_score = sum(chunk) / len(chunk)
    
    # Calculate timestamp for this chunk
    timestamp_ms = i * 32
    seconds = timestamp_ms // 1000
    millis = timestamp_ms % 1000
    
    if avg_score > THRESHOLD:
        bar = "##########"
        label = f"{avg_score:.2f}"
    else:
        bar = "----------"
        label = f"{avg_score:.2f}"
        
    print(f"[{seconds:02d}.{millis:03d}] {bar} ({label})")

print("=" * 60)
print("TEST COMPLETE")
print("If you see clear blocks of '##########' where you spoke,")
print("and '----------' where you paused, the model is working perfectly.")