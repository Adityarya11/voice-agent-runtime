import os
import time
import logging
from faster_whisper import WhisperModel

logger = logging.getLogger("InferenceEngine.STT")

class Transcriber:
    def __init__(self, model_size: str = "base", device: str = "cpu", compute_type: str = "int8", language:str | None = "en"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.model = None
        self._load_model()

    def _load_model(self):
        try:
            logger.info(f"Initializing Faster-Whisper model context: '{self.model_size}' running on '{self.device}'...")
            self.model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
            logger.info("Faster-Whisper parameters bound and loaded successfully.")
        except Exception as e:
            logger.critical(f"Failed to fetch or construct local whisper runtime weights: {e}")
            raise e

    def transcribe(self, audio_path: str) -> str:
        if not os.path.exists(audio_path):
            logger.error(f"Inbound audio execution mapping missing: {audio_path}")
            return ""

        if self.model is None:
            raise RuntimeError("Whisper model is not loaded")

        try:
            start_time = time.time()

            segments, info = self.model.transcribe(
                audio_path,
                beam_size=5,
                vad_filter=True,
                language=self.language
            )
            
            # Combine execution chunks cleanly
            text_output = " ".join([segment.text for segment in segments]).strip()
            
            duration = time.time() - start_time
            if self.language:
                logger.info(
                    f"Transcribed audio track in {duration:.3f}s "
                    f"| Language pinned: {self.language}"
                )
            else:
                logger.info(
                    f"Transcribed audio track in {duration:.3f}s "
                    f"| Language parsed: {info.language} "
                    f"({info.language_probability:.2f})"
                )
            return text_output
        except Exception as e:
            logger.error(f"Error during audio sample transcribing phase: {e}", exc_info=True)
            return ""