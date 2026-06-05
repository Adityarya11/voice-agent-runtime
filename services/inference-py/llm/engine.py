import time
import logging
import ollama

logger = logging.getLogger("InferenceEngine.LLM")

_FALLBACK_SYSTEM_PROMPT = (
    "You are a concise voice assistant. Limit replies to two sentences maximum."
)


class LLMEngine:
    def __init__(self, model_target: str = "qwen2.5:3b"):
        self.model_target = model_target
        self._verify_local_presence()

    def _verify_local_presence(self):
        logger.info(f"Verifying model target: '{self.model_target}'")
        try:
            ollama.show(model=self.model_target)
            logger.info(f"Model '{self.model_target}' verified.")
        except Exception:
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
            start_time = time.time()
            response = ollama.chat(
                model=self.model_target,
                messages=messages,
                stream=False
            )
            latency = time.time() - start_time
            content = response.get("message", {}).get("content", "").strip()
            logger.info(f"Inference completed in {latency:.3f}s")
            return content
        except Exception as e:
            logger.error(f"Inference failure: {e}", exc_info=True)
            return "I encountered an error processing your request. Please try again."