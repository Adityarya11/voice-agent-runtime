import os
import re
import time
import urllib.request

import ollama
import sounddevice as sd
import speech_recognition as sr

from faster_whisper import WhisperModel
from piper.voice import PiperVoice

# ==========================================
# 0. Download Piper Model (One-time)
# ==========================================

MODEL_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "en/en_US/lessac/medium/en_US-lessac-medium.onnx"
)

CONFIG_URL = MODEL_URL + ".json"

MODEL_FILE = "models/en_US-lessac-medium.onnx"
CONFIG_FILE = "models/en_US-lessac-medium.onnx.json"

if not os.path.exists(MODEL_FILE):
    print("[System] Downloading Piper voice model...")

    urllib.request.urlretrieve(MODEL_URL, MODEL_FILE)
    urllib.request.urlretrieve(CONFIG_URL, CONFIG_FILE)

    print("[System] Piper model downloaded.")

# ==========================================
# 1. Load Models
# ==========================================

print("[System] Loading Faster-Whisper...")
stt_model = WhisperModel(
    "base",
    device="cpu",
    compute_type="int8"
)

print("[System] Loading Piper...")
tts_voice = PiperVoice.load(MODEL_FILE)

print("[System] Ready.")

# ==========================================
# 2. Audio Recording
# ==========================================

def record_audio(filename="temp_mic.wav"):
    recognizer = sr.Recognizer()

    with sr.Microphone() as source:
        print("\n[Microphone] Calibrating...")
        recognizer.adjust_for_ambient_noise(source, duration=1)

        print("[Microphone] Listening...")
        audio = recognizer.listen(source)

    with open(filename, "wb") as f:
        f.write(audio.get_wav_data())

    return filename

# ==========================================
# 3. Speak Text Using Piper
# ==========================================

def speak_text(text, stream):
    """
    Convert text to speech and play immediately.
    """

    if not text.strip():
        return

    try:
        for chunk in tts_voice.synthesize(text):
            stream.write(chunk.audio_int16_bytes)

    except Exception as e:
        print(f"\n[TTS Error] {e}")

# ==========================================
# 4. Main Loop
# ==========================================

def main():

    chat_history = [
        {
            "role": "system",
            "content": (
                "You are Ravi, a concise AI voice assistant. "
                "Keep responses under 2 short sentences."
            ),
        }
    ]

    while True:

        try:
            input(
                "\nPress ENTER to record "
                "(Ctrl+C to quit)..."
            )

            # -------------------------
            # Record
            # -------------------------

            audio_file = record_audio()

            # -------------------------
            # STT
            # -------------------------

            print("[STT] Transcribing...")

            stt_start = time.time()

            segments, _ = stt_model.transcribe(
                audio_file,
                beam_size=5,
            )

            user_text = "".join(
                segment.text
                for segment in segments
            ).strip()

            stt_time = time.time() - stt_start

            print(
                f"\n🗣️ You said: '{user_text}' "
                f"({stt_time:.2f}s)"
            )

            if not user_text:
                print("[System] No speech detected.")
                continue

            chat_history.append(
                {
                    "role": "user",
                    "content": user_text,
                }
            )

            # -------------------------
            # Open Audio Stream
            # -------------------------

            stream = sd.RawOutputStream(
                samplerate=tts_voice.config.sample_rate,
                channels=1,
                dtype="int16",
            )

            stream.start()

            # -------------------------
            # LLM
            # -------------------------

            print("🤖 AI says: ", end="", flush=True)

            llm_stream = ollama.chat(
                model="qwen2.5:3b",
                messages=chat_history,
                stream=True,
            )

            full_response = ""
            text_buffer = ""

            for chunk in llm_stream:

                token = chunk["message"]["content"]

                print(token, end="", flush=True)

                full_response += token
                text_buffer += token

                # ----------------------------------
                # Speak complete sentences
                # ----------------------------------

                while True:

                    match = re.search(
                        r"[.!?]",
                        text_buffer,
                    )

                    if not match:
                        break

                    end_idx = match.end()

                    sentence = (
                        text_buffer[:end_idx]
                        .strip()
                    )

                    text_buffer = (
                        text_buffer[end_idx:]
                    )

                    if sentence:
                        speak_text(
                            sentence,
                            stream,
                        )

            # ----------------------------------
            # Speak leftover text
            # ----------------------------------

            if text_buffer.strip():
                speak_text(
                    text_buffer.strip(),
                    stream,
                )

            print()

            stream.stop()
            stream.close()

            # -------------------------
            # Save Context
            # -------------------------

            chat_history.append(
                {
                    "role": "assistant",
                    "content": full_response,
                }
            )

            # -------------------------
            # Cleanup
            # -------------------------

            if os.path.exists(audio_file):
                os.remove(audio_file)

        except KeyboardInterrupt:
            print("\n\n[System] Exiting...")
            break

        except Exception as e:
            print(f"\n[Error] {e}")

# ==========================================
# Entry
# ==========================================

if __name__ == "__main__":
    main()