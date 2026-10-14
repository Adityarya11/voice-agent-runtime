import io
import json
import os
import sys
import wave

import numpy as np
import scipy.signal

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPER_MODEL = os.path.join(
    REPO_ROOT, "services", "inference-py", "models", "en_US-lessac-medium.onnx"
)
OUT_DIR = os.path.join(REPO_ROOT, "test_data", "bench")
TARGET_SR = 8000

UTTERANCES = [
    ("u01", "Hello."),
    ("u02", "Hi, I'd like to book an appointment."),
    ("u03", "Yes, I'm a returning patient."),
    ("u04", "No, this is my first visit."),
    ("u05", "Can I come in tomorrow morning?"),
    ("u06", "What are your opening hours on weekends?"),
    ("u07", "Where is the clinic located, and is there parking nearby?"),
    ("u08", "Thanks, that's all I needed."),
    (
        "u09",
        "My tooth has been hurting since last night, and I think I need to see "
        "someone as soon as possible.",
    ),
    (
        "u10",
        "Do you accept dental insurance, and if so, which providers do you work with?",
    ),
    (
        "u11",
        "I need to reschedule my cleaning from Thursday to sometime next week, "
        "preferably in the afternoon after two.",
    ),
    (
        "u12",
        "Actually, could you tell me how much a regular checkup costs without "
        "insurance, and whether you offer payment plans for bigger treatments "
        "like crowns or root canals?",
    ),
]


def ulaw_encode(pcm16: np.ndarray) -> np.ndarray:
    bias, clip = 0x84, 32635
    s = pcm16.astype(np.int32)
    sign = np.where(s < 0, 0x80, 0)
    s = np.minimum(np.abs(s), clip) + bias
    exponent = np.floor(np.log2(s >> 7)).astype(np.int32)
    mantissa = (s >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def ulaw_decode(codes: np.ndarray) -> np.ndarray:
    u = (~codes.astype(np.int32)) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -sample, sample).astype(np.int16)


def trim_silence(pcm: np.ndarray, threshold: int = 300, margin_ms: int = 20) -> np.ndarray:
    loud = np.flatnonzero(np.abs(pcm.astype(np.int32)) > threshold)
    if loud.size == 0:
        return pcm
    margin = int(margin_ms / 1000 * TARGET_SR)
    start = max(0, int(loud[0]) - margin)
    end = min(len(pcm), int(loud[-1]) + 1 + margin)
    return pcm[start:end]


def to_telephone_pcm(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        source_sr = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    gcd = np.gcd(source_sr, TARGET_SR)
    resampled = scipy.signal.resample_poly(
        samples.astype(np.float32) / 32768.0, TARGET_SR // gcd, source_sr // gcd
    )
    pcm = np.clip(resampled * 32767.0, -32768, 32767).astype(np.int16)
    return trim_silence(ulaw_decode(ulaw_encode(pcm)))


def main() -> int:
    from piper.voice import PiperVoice

    voice = PiperVoice.load(PIPER_MODEL)
    os.makedirs(OUT_DIR, exist_ok=True)

    manifest = []
    for utt_id, text in UTTERANCES:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        pcm = to_telephone_pcm(buf.getvalue())

        path = os.path.join(OUT_DIR, f"{utt_id}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_SR)
            w.writeframes(pcm.tobytes())

        duration = len(pcm) / TARGET_SR
        manifest.append({"id": utt_id, "text": text, "duration_s": round(duration, 3)})
        print(f"{utt_id}  {duration:5.2f}s  {text}")

    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
