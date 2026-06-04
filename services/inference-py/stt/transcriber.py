import time 
import logging
from faster_whisper import WhisperModel

class Transcriber: 
    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        logging.info(f"Loading Faster-Whishper ({model_size}) on {device}")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, wav_path:str) -> str: 
        start = time.time()
        segments, _ = self.model.transcribe(wav_path, beam_size=5)
        transcript = "".join(segment.text for segment in segments).strip()
        logging.info("STT completed in %.2fs", time.time() - start)

        return transcript