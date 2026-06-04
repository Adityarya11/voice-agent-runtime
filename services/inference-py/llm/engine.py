import time
import logging
import ollama

logger = logging.getLogger("InferenceEngine.LLM")

class LLMEngine:
    def __init__(self, model_target: str = "qwen2.5:3b"):
        self.model_target = model_target
        self._verify_local_presence()

    def _verify_local_presence(self):
        logger.info(f"Checking availability of underlying model runtime targets: '{self.model_target}'")
        try:
            # Quick check against local ollama daemon instance state
            ollama.show(model=self.model_target)
            logger.info(f"Model target verification passed: '{self.model_target}' is ready.")
        except Exception:
            logger.warning(f"Target model '{self.model_target}' was not explicitly verified. Ensure 'ollama pull {self.model_target}' has run.")

    def generate(self, prompt: str, system_override: str | None = None) -> str:
        system_instruction = system_override or "You are a concise voice assistant. Limit replies to two sentences max."
        
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ]

        try:
            start_time = time.time()
            response = ollama.chat(model=self.model_target, messages=messages, stream=False)
            
            execution_latency = time.time() - start_time
            content = response.get("message", {}).get("content", "").strip()
            
            logger.info(f"Inference execution cycle wrapped up in {execution_latency:.3f}s")
            return content
        except Exception as e:
            logger.error(f"Critical failure across local model inference loop processing: {e}", exc_info=True)
            return "I ran into a temporary error processing your request. Could you say that again?"