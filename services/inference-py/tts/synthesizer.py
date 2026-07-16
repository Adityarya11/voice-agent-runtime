import io
import logging
import os
import time
import wave

from piper.voice import PiperVoice

logger = logging.getLogger("InferenceEngine.TTS")


class Synthesizer:
    def __init__(self, model_filename: str = "en_US-lessac-medium.onnx"):
        self.model_filename = model_filename

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.model_path = os.path.join(base_dir, "models", self.model_filename)

        self.voice = None
        self._load_synthesizer()

    def _load_synthesizer(self):
        if not os.path.exists(self.model_path):
            root_fallback = os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ),
                self.model_filename,
            )
            if os.path.exists(root_fallback):
                self.model_path = root_fallback
            else:
                logger.error(f"Piper ONNX model not found: {self.model_path}")
                return

        try:
            logger.info(f"Loading Piper voice model from: {self.model_path}...")
            self.voice = PiperVoice.load(self.model_path)
            logger.info("Piper voice model loaded.")
        except (OSError, RuntimeError) as e:
            logger.error(f"Failed to load Piper model: {e}", exc_info=True)

    def synthesize(self, text: str) -> bytes:
        if not self.voice:
            logger.error("Synthesis request dropped: Piper model not loaded.")
            return b""

        try:
            start_time = time.time()
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_out:
                self.voice.synthesize_wav(text, wav_out)
            raw_bytes = buffer.getvalue()
            duration = time.time() - start_time
            logger.info(f"TTS completed in {duration:.3f}s | {len(raw_bytes)} bytes.")
            return raw_bytes
        except (RuntimeError, wave.Error) as e:
            logger.error(f"TTS synthesis failed: {e}", exc_info=True)
            return b""
