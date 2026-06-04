import time
import logging 
import ollama

class LLMEngine: 
    def __init__(self, model_name="qwen2.5:3b"):
        self.model_name = model_name
        logging.info(f"LLM Engine initiated, with {self.model_name}")

    def generate_stream(self, user_text: str):
        ## enhancement: eventually move this prompt to the agent profile configs

        message = [
            {
                "role": "system",
                "content": "You are Aditya, a concise voice assistant. Respond in at most two short sentences."
            }, 
            {
                "role": "user", 
                "content": user_text
            }
        ]

        start = time.time()
        response_stream = ollama.chat(
            model=self.model_name,
            messages=message, 
            stream=True
        )

        logging.info("LLM inference started in %.2fs", time.time() - start)
        
        return response_stream