import io
import os
import sys
import time
import signal
import logging
from concurrent import futures
import grpc

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
        logger.info("Initializing inference engines...")
        self.transcriber = Transcriber()
        self.llm = LLMEngine()
        self.tts = Synthesizer()
        logger.info("All inference modules loaded.")

    def StreamEvents(self, request_iterator, context):
        logger.info("Incoming gRPC stream connected.")

        audio_buffer = io.BytesIO()
        session_id = "unknown"
        system_prompt = None
        agent_name = "unknown"

        try:
            for event in request_iterator:
                session_id = event.session_id

                if event.HasField("control"):
                    control = event.control
                    if control.profile.system_prompt:
                        system_prompt = control.profile.system_prompt
                        agent_name = control.profile.agent_name
                        logger.info(
                            f"[{session_id}] Profile received — agent: '{agent_name}'"
                        )

                elif event.HasField("audio"):
                    audio_buffer.write(event.audio.data)

            if audio_buffer.tell() == 0:
                logger.warning(f"[{session_id}] Stream closed with no audio payload.")
                return

            logger.info(
                f"[{session_id}] Processing {audio_buffer.tell()} bytes "
                f"under profile '{agent_name}'"
            )

            temp_wav = f"temp_in_{session_id}.wav"
            with open(temp_wav, "wb") as f:
                f.write(audio_buffer.getvalue())

            try:
                stt_start = time.time()
                user_text = self.transcriber.transcribe(temp_wav)
                stt_latency = time.time() - stt_start

                if not user_text:
                    logger.warning(f"[{session_id}] Empty transcription result.")
                    return

                logger.info(
                    f"[{session_id}] STT: '{user_text}' "
                    f"| latency: {stt_latency:.3f}s"
                )

                session_start = time.time()
                chunks_sent = 0

                for sentence in self.llm.generate_stream(
                    user_text,
                    system_override=system_prompt
                ):
                    tts_start = time.time()
                    wav_bytes = self.tts.synthesize(sentence)
                    tts_latency = time.time() - tts_start

                    if not wav_bytes:
                        logger.warning(
                            f"[{session_id}] TTS returned empty bytes "
                            f"for sentence: '{sentence}'"
                        )
                        continue

                    logger.info(
                        f"[{session_id}] TTS chunk: {len(wav_bytes)} bytes "
                        f"| latency: {tts_latency:.3f}s"
                    )

                    for i in range(0, len(wav_bytes), CHUNK_SIZE):
                        chunk = wav_bytes[i: i + CHUNK_SIZE]
                        yield agent_pb2.Event(
                            session_id=session_id,
                            audio=agent_pb2.AudioChunk(data=chunk)
                        )
                        chunks_sent += 1

                total_latency = time.time() - session_start
                logger.info(
                    f"[{session_id}] Session complete — "
                    f"chunks sent: {chunks_sent} | "
                    f"total response time: {total_latency:.3f}s"
                )

            finally:
                if os.path.exists(temp_wav):
                    os.remove(temp_wav)

        except grpc.RpcError as e:
            logger.error(f"[{session_id}] gRPC error: {e}")
        except Exception as e:
            logger.critical(
                f"[{session_id}] Unhandled failure in session execution: {e}",
                exc_info=True
            )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ]
    )
    agent_pb2_grpc.add_VoiceAgentServicer_to_server(VoiceAgentServicer(), server)
    server.add_insecure_port("[::]:50051")
    logger.info("Inference Engine running on port :50051")

    server.start()

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received. Draining server...")
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    server.wait_for_termination()


if __name__ == "__main__":
    serve()