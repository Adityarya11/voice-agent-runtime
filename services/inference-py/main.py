import io
import os
import sys
import signal
import logging
from concurrent import futures
import grpc

# Fixing python path for grpc generated imports
sys.path.append(os.path.join(os.path.dirname(__file__), "grpc_server"))
import agent_pb2
import agent_pb2_grpc

from stt.transcriber import Transcriber
from llm.engine import LLMEngine
from tts.synthesizer import Synthesizer

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("InferenceEngine")

CHUNK_SIZE = 4096

class VoiceAgentServicer(agent_pb2_grpc.VoiceAgentServicer):
    def __init__(self):
        logger.info("Initializing baseline inference engines...")
        self.transcriber = Transcriber()
        self.llm = LLMEngine()
        self.tts = Synthesizer()
        logger.info("All inference modules successfully bound and warm.")

    def StreamEvents(self, request_iterator, context):
        logger.info("Incoming gRPC bi-directional stream connected.")
        audio_buffer = io.BytesIO()
        session_id = "unknown"

        try:
            for event in request_iterator:
                session_id = event.session_id
                if event.HasField("audio"):
                    audio_buffer.write(event.audio.data)
                
            # Execute processing safely if we collected content
            if audio_buffer.tell() == 0:
                logger.warning(f"Session {session_id} stream closed with zero audio payloads.")
                return

            logger.info(f"Processing accumulated stream data for session: {session_id} ({audio_buffer.tell()} bytes)")
            
            # Temporary disk boundary for localized model processing
            temp_wav = f"temp_in_{session_id}.wav"
            with open(temp_wav, "wb") as f:
                f.write(audio_buffer.getvalue())

            try:
                # 1. Speech to Text Boundary
                user_text = self.transcriber.transcribe(temp_wav)
                if not user_text:
                    logger.warning(f"[{session_id}] Null or unparseable transcription segment.")
                    return
                logger.info(f"[{session_id}] STT Result: {user_text}")

                # 2. Large Language Model Boundary
                response_text = self.llm.generate(user_text)
                logger.info(f"[{session_id}] LLM Response: {response_text}")

                # 3. Text to Speech Boundary
                wav_bytes = self.tts.synthesize(response_text)

                # 4. Chunked Outbound Streaming Payload
                for i in range(0, len(wav_bytes), CHUNK_SIZE):
                    chunk = wav_bytes[i : i + CHUNK_SIZE]
                    yield agent_pb2.Event(
                        session_id=session_id,
                        audio=agent_pb2.AudioChunk(data=chunk)
                    )
                logger.info(f"[{session_id}] Outbound audio transmission successfully finalized.")

            finally:
                if os.path.exists(temp_wav):
                    os.remove(temp_wav)

        except grpc.RpcError as e:
            logger.error(f"gRPC Layer Connection Break for Session {session_id}: {e}")
        except Exception as e:
            logger.critical(f"Unhandled Runtime Failure in Session Execution {session_id}: {e}", exc_info=True)


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024)
        ]
    )
    agent_pb2_grpc.add_VoiceAgentServicer_to_server(VoiceAgentServicer(), server)
    server.add_insecure_port("[::]:50051")
    logger.info("Inference Engine Core running natively on port :50051")
    
    server.start()

    # Graceful stop hook
    def handle_shutdown(signum, frame):
        logger.info("Received termination signal. Draining server context smoothly...")
        server.stop(grace=5)
        logger.info("Inference Engine terminated clean.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    server.wait_for_termination()

if __name__ == "__main__":
    serve()