import io
import os
import sys
import logging 
from concurrent import futures
import grpc

from stt.transcriber import Transcriber
from llm.engine import LLMEngine
from tts.synthesizer import Synthesizer

sys.path.append(os.path.join(os.path.dirname(__file__), 'grpc_server'))
import agent_pb2
import agent_pb2_grpc

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
CHUNK_SIZE = 4096

class VoiceAgentServicer(agent_pb2_grpc.VoiceAgentServicer):
    def __init__(self): 
        self.transcriber = Transcriber() 
        self.llm = LLMEngine()
        self.tts = Synthesizer()

    def StreamEvents(self, request_iterator, context):
        logging.info("Stream connected")
        audio_buffer = io.BytesIO()
        session_id = "unknown"

        system_prompt = "You are a concise voice assistant. Respond in one short sentence."
        
        for event in request_iterator: 
            session_id = event.session_id

            if event.HasField("control"):
                if event.control.type == agent_pb2.ControlSignal.START_SESSION:
                    profile = event.control.profile
                    if profile.system_prompt:
                        system_prompt = profile.system_prompt
                        logging.info(f"Session {session_id} initialized as: {profile.agent_name}")

            elif event.HasField("audio"):
                audio_buffer.write(event.audio.data)

        logging.info("Received audio from session %s", session_id)
        temp_wav = f"temp_{session_id}.wav"

        try: 
            with open(temp_wav, "wb") as f: 
                f.write(audio_buffer.getvalue())

            user_text = self.transcriber.transcribe(temp_wav)
            if not user_text: 
                logging.warning("No speech detected")
                return
            logging.info("Transcript: %s", user_text)

            response_stream = self.llm.generate_stream(system_prompt=system_prompt, user_text=user_text)

            full_response = []
            for chunk in response_stream: 
                full_response.append(chunk["message"]["content"])
            response_text = "".join(full_response)

            logging.info("Response: %s", response_text)


            wav_bytes = self.tts.synthesize_to_bytes(response_text)

            for i in range(0, len(wav_bytes), CHUNK_SIZE):
                chunk = wav_bytes[i: i + CHUNK_SIZE]

                yield agent_pb2.Event(
                    session_id=session_id,
                    audio=agent_pb2.AudioChunk(
                        data=chunk,
                    ),
                )
            
            logging.info("Completed session %s", session_id)

        finally:
            if os.path.exists(temp_wav):
                os.remove(temp_wav)



def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    agent_pb2_grpc.add_VoiceAgentServicer_to_server(VoiceAgentServicer(), server)
    server.add_insecure_port("[::]:50051")
    logging.info("Inference Engine listening on :50051")

    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve()