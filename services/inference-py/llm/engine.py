import logging
import re
import time
from datetime import datetime

import httpx
import ollama

logger = logging.getLogger("InferenceEngine.LLM")

_FALLBACK_SYSTEM_PROMPT = (
    "You are a concise voice assistant. Limit replies to two sentences maximum."
)

_SENTENCE_ENDINGS = re.compile(r"[.!?]")
_MIN_CHUNK_LENGTH = 8


class LLMEngine:
    def __init__(self, model_target: str = "qwen2.5:3b"):
        self.model_target = model_target
        self._verify_local_presence()

    def _verify_local_presence(self):
        logger.info(f"Verifying model target: '{self.model_target}'")
        try:
            ollama.show(model=self.model_target)
            logger.info(f"Model '{self.model_target}' verified.")
        except (ollama.ResponseError, httpx.RequestError):
            logger.warning(
                f"Model '{self.model_target}' could not be verified. "
                f"Ensure 'ollama pull {self.model_target}' has been run."
            )

    def generate(self, prompt: str, system_override: str | None = None) -> str:
        system_prompt = system_override if system_override else _FALLBACK_SYSTEM_PROMPT

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            response = ollama.chat(model=self.model_target, messages=messages)
            return response.get("message", {}).get("content", "")
        except (ollama.ResponseError, httpx.RequestError) as e:
            logger.error(f"Inference failure: {e}", exc_info=True)
            return "I encountered an error processing your request."

    def _log_ttft(self, request_start: float):
        ttft = time.perf_counter() - request_start
        logger.info(
            f"First token at "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"(TTFT: {ttft:.3f}s)"
        )

    def _extract_sentences(self, buffer: str) -> tuple[list[str], str]:
        """Extracts complete sentences from the buffer and returns them alongside the remaining text."""
        sentences = []
        while True:
            if len(buffer) < _MIN_CHUNK_LENGTH:
                break

            match = _SENTENCE_ENDINGS.search(buffer, _MIN_CHUNK_LENGTH)
            if not match:
                break

            end_index = match.end()
            sentence = buffer[:end_index].strip()
            buffer = buffer[end_index:]
            sentences.append(sentence)

        return sentences, buffer

    def generate_stream(self, prompt: str, system_override: str | None = None):

        system_prompt = system_override if system_override else _FALLBACK_SYSTEM_PROMPT

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        buffer = ""

        request_start = time.perf_counter()
        first_chunk_logged = False

        try:
            stream = ollama.chat(
                model=self.model_target, messages=messages, stream=True, keep_alive=-1
            )

            for chunk in stream:
                token = chunk.get("message", {}).get("content", "")
                if not token:
                    continue

                if not first_chunk_logged:
                    self._log_ttft(request_start)
                    first_chunk_logged = True

                buffer += token

                sentences, buffer = self._extract_sentences(buffer)
                for sentence in sentences:
                    logger.info(f"Sentence chunk ready: '{sentence}'")
                    yield sentence

            if buffer.strip() and len(buffer.strip()) >= _MIN_CHUNK_LENGTH:
                logger.info(f"Remaining chunk: '{buffer.strip()}'")
                yield buffer.strip()

        except (ollama.ResponseError, httpx.RequestError) as e:
            logger.error(f"Streaming inference failure: {e}", exc_info=True)
            yield "I encountered an error processing your request."
