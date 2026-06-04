import io
import os
import wave
import time
import logging
from piper.voice import PiperVoice

logger = logging.getLogger("InferenceEngine.TTS")

class Synthesizer:
    def __init__(self, model_filename: str = "en_US-lessac-medium.onnx"):
        # Automatically look for assets tucked inside our clean structure
        self.model_filename = model_filename

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.model_path = os.path.join(base_dir, "models", self.model_filename)

        self.voice = None
        self._load_synthesizer()

    def _load_synthesizer(self):
        if not os.path.exists(self.model_path):
            root_fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                         self.model_filename)
            
            if os.path.exists(root_fallback):
                self.model_path = root_fallback
            else:
                logger.error(f"Piper ONNX model weights not found at path destination: {self.model_path}")
                return

        try:
            logger.info(f"Loading native internal ONNX synthesis graph from: {self.model_path}...")
            self.voice = PiperVoice.load(self.model_path)
            logger.info("Piper voice processing structures safely mapped into system memory.")
        except Exception as e:
            logger.error(f"Failed to cleanly initialize ONNX synthesizer layout weights: {e}", exc_info=True)

    def synthesize(self, text: str) -> bytes:
        if not self.voice:
            logger.error("Synthesis request dropped: Piper model engine graph uninitialized or invalid.")
            return b""

        try:
            start_time = time.time()
            buffer = io.BytesIO()
            
            with wave.open(buffer, "wb") as wav_out:
                # Write audio arrays straight into our buffer
                self.voice.synthesize_wav(text, wav_out)
                
            raw_output_payload = buffer.getvalue()
            duration = time.time() - start_time
            logger.info(f"Audio track generation completed in {duration:.3f}s | Payload size: {len(raw_output_payload)} bytes.")
            return raw_output_payload
        except Exception as e:
            logger.error(f"Error encountered during runtime voice synthesis pipeline execution: {e}", exc_info=True)
            return b""