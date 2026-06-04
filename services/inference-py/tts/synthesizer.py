import time
import logging
import io 
import wave 
from piper.voice import PiperVoice

class Synthesizer: 
    def __init__(self, model_path="models/en_US-lessac-medium.onnx"):
        logging.info(f"Loading Piper voice tts.. from {model_path}")
        self.voice = PiperVoice.load(model_path)

    def synthesize_to_bytes(self, text:str) -> bytes:
        start = time.time()
        wav_buffer = io.BytesIO()

        with wave.open(wav_buffer, "wb") as wavFile: 
            self.voice.synthesize_wav(text, wavFile)

        wav_bytes = wav_buffer.getvalue()
        logging.info("TTS completed in %.2fs", time.time() - start)

        return wav_bytes