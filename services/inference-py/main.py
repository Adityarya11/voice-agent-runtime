import io
import os
import sys
import time
import signal
import logging
import threading
import queue
from concurrent import futures
import grpc

sys.path.append(os.path.join(os.path.dirname(__file__), "grpc_server"))
import agent_pb2
import agent_pb2_grpc

from stt import Transcriber
from llm import LLMEngine
from tts import Synthesizer
from vad import (
    AudioPreprocessor, 
    frames_to_wav, 
    VADCommand,
    VADDetector
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("InferenceEngine")

CHUNK_SIZE = 4096
_SHUTDOWN = object()
VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
SOURCE_SAMPLE_RATE = 44100


class VoiceAgentServicer(agent_pb2_grpc.VoiceAgentServicer):
    def __init__(self):
        logger.info("Initializing inference engines...")
        self.transcriber = Transcriber()
        self.llm = LLMEngine()
        self.tts = Synthesizer()
        logger.info("All inference modules loaded.")

    def _run_utterance(self, session_id, utterance_bytes, system_prompt, outbound_queue, context, utterance_done_event):

        temp_wav = f"temp_in_{session_id}_{int(time.time() * 1000)}.wav"

        try:
            with open(temp_wav, "wb") as f:
                f.write(utterance_bytes)

            stt_start = time.time()
            user_text = self.transcriber.transcribe(temp_wav)
            stt_latency = time.time() - stt_start

            if not user_text:
                logger.warning(f"[{session_id}] Empty transcription for utterance.")
                return

            logger.info(
                f"[{session_id}] STT: '{user_text}' | latency: {stt_latency:.3f}s"
            )

            for sentence in self.llm.generate_stream(
                user_text,
                system_override=system_prompt
            ):
                if not context.is_active():
                    logger.warning(
                        f"[{session_id}] gRPC context cancelled mid-utterance. "
                        f"Stopping inference."
                    )
                    return

                wav_bytes = self.tts.synthesize(sentence)
                if not wav_bytes:
                    logger.warning(
                        f"[{session_id}] TTS returned empty bytes for: '{sentence}'"
                    )
                    continue

                for i in range(0, len(wav_bytes), CHUNK_SIZE):
                    chunk = wav_bytes[i: i + CHUNK_SIZE]
                    outbound_queue.put(agent_pb2.Event(
                        session_id=session_id,
                        audio=agent_pb2.AudioChunk(data=chunk)
                    ))

            logger.info(f"[{session_id}] Utterance response complete.")

        except Exception as e:
            logger.error(
                f"[{session_id}] Utterance processing failure: {e}", exc_info=True
            )
        finally:
            if os.path.exists(temp_wav):
                os.remove(temp_wav)
            utterance_done_event.set()

    def _dispatch_utterance(self, session_id, vad, system_prompt, outbound_queue, context, utterance_done_event,):
        
        frames = vad.get_utterance_frames()
        if len(frames) == 0:
            logger.warning(f"[{session_id}] Boundary fired with empty VAD buffer, ignoring.")
            return

        utterance_bytes = frames_to_wav(frames, AudioPreprocessor.TARGET_SR)
        utterance_done_event.clear()

        logger.info(
            f"[{session_id}] Utterance boundary ({len(frames)} smaples, "
            f"{len(frames) / AudioPreprocessor.TARGET_SR:.2f}s). Dispatching inference."
        )

        threading.Thread(
            target=self._run_utterance,
            args=(
                session_id,
                utterance_bytes,
                system_prompt,
                outbound_queue,
                context,
                utterance_done_event,
            ),
            daemon=True,
        ).start()

    def _read_pump(self, request_iterator, session_id_holder, outbound_queue, context):
        system_prompt = None
        utterance_done_event = threading.Event()
        utterance_done_event.set()

        vad = VADDetector(VAD_MODEL_PATH)
        preprocessor = AudioPreprocessor(source_sr=SOURCE_SAMPLE_RATE)

        try:
            for event in request_iterator:
                session_id_holder["id"] = event.session_id
                session_id = session_id_holder["id"]

                if event.HasField("control"):
                    control = event.control

                    if control.profile.system_prompt:
                        system_prompt = control.profile.system_prompt
                        logger.info(
                            f"[{session_id}] Profile received — "
                            f"agent: '{control.profile.agent_name}'"
                        )

                    if control.type == agent_pb2.ControlSignal.END_OF_UTTERANCE:
                        if not utterance_done_event.is_set():
                            utterance_done_event.wait()

                        self._dispatch_utterance(
                            session_id, vad, system_prompt,
                            outbound_queue, context, utterance_done_event,
                        )

                elif event.HasField("audio"):
                    for frame in preprocessor.push(event.audio.data):
                        command = vad.process_frames(frame)

                        if command == VADCommand.START_SPEECH:
                            logger.info(f"[{session_id}] VAD: speech started.")

                        elif command == VADCommand.END_OF_UTTERANCE:
                            if not utterance_done_event.is_set():
                                logger.warning(
                                    f"[{session_id}] VAD boundary while utterance "
                                    f"in progress. Waiting."
                                )
                                utterance_done_event.wait()

                            self._dispatch_utterance(
                                session_id, vad, system_prompt,
                                outbound_queue, context, utterance_done_event,
                            )

        except Exception as e:
            logger.error(f"[{session_id_holder['id']}] read_pump failure: {e}", exc_info=True)
        finally:
            logger.info(
                f"[{session_id_holder['id']}] Inbound stream closed. Signaling shutdown."
            )
            outbound_queue.put(_SHUTDOWN)

    def StreamEvents(self, request_iterator, context):
        logger.info("Incoming gRPC stream connected (duplex mode).")

        outbound_queue = queue.Queue()
        session_id_holder = {"id": "unknown"}

        pump_thread = threading.Thread(
            target=self._read_pump,
            args=(request_iterator, session_id_holder, outbound_queue, context),
            daemon=True,
        )
        pump_thread.start()

        while True:
            event = outbound_queue.get()
            if event is _SHUTDOWN:
                break
            yield event

        logger.info(f"[{session_id_holder['id']}] StreamEvents generator exiting.")


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=20),
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