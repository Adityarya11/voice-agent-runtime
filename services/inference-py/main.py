import logging
import os
import queue
import signal
import sys
import threading
from concurrent import futures
from dataclasses import dataclass, field

import grpc

sys.path.append(os.path.join(os.path.dirname(__file__), "grpc_server"))
import agent_pb2
import agent_pb2_grpc

from llm import LLMEngine
from stt import Transcriber
from tts import Synthesizer
from vad import (
    AudioPreprocessor,
    VADCommand,
    VADDetector,
    frames_to_wav,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("InferenceEngine")

CHUNK_SIZE = 4096
_SHUTDOWN = object()
VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
SOURCE_SAMPLE_RATE = 44100


@dataclass
class SessionContext:
    session_id: str
    outbound_queue: queue.Queue
    grpc_context: grpc.ServicerContext
    utterance_done_event: threading.Event = field(default_factory=threading.Event)
    system_prompt: str | None = None


class VoiceAgentServicer(agent_pb2_grpc.VoiceAgentServicer):
    def __init__(self):
        logger.info("Initializing inference engines...")
        self.transcriber = Transcriber()
        self.llm = LLMEngine()
        self.tts = Synthesizer()
        logger.info("All inference modules loaded.")

    def _run_utterance(self, ctx: SessionContext, utterance_bytes: bytes) -> None:
        try:
            user_text = self.transcriber.transcribe(utterance_bytes)

            if not user_text:
                logger.warning(f"[{ctx.session_id}] Empty transcription, skipping inference.")
                return

            logger.info(f"[{ctx.session_id}] STT: '{user_text}'")

            for sentence in self.llm.generate_stream(user_text, system_override=ctx.system_prompt):
                if not ctx.grpc_context.is_active():
                    logger.warning(
                        f"[{ctx.session_id}] gRPC context cancelled mid-utterance. "
                        f"Stopping inference"
                    )
                    return

                wav_bytes = self.tts.synthesize(sentence)
                if not wav_bytes:
                    logger.warning(f"[{ctx.session_id}] TTS returned empty bytes for: '{sentence}'")
                    continue

                for i in range(0, len(wav_bytes), CHUNK_SIZE):
                    chunk = wav_bytes[i: i + CHUNK_SIZE]
                    ctx.outbound_queue.put(agent_pb2.Event(
                        session_id=ctx.session_id,
                        audio=agent_pb2.AudioChunk(data=chunk),
                    ))

            logger.info(f"[{ctx.session_id}] Utterance response complete.")

        except grpc.RpcError as e:
            logger.error(f"[{ctx.session_id}] gRPC error during utterance: {e}", exc_info=True)
        except RuntimeError as e:
            logger.error(f"[{ctx.session_id}] Inference runtime failure: {e}", exc_info=True)
        finally:
            ctx.utterance_done_event.set()

    def _dispatch_utterance(self, ctx: SessionContext, vad: VADDetector) -> None:
        frames = vad.get_utterance_frames()
        if len(frames) == 0:
            logger.warning(f"[{ctx.session_id}] Boundary fired with empty VAD buffer, ignoring.")
            return

        utterance_bytes = frames_to_wav(frames, AudioPreprocessor.TARGET_SR)
        ctx.utterance_done_event.clear()

        logger.info(
            f"[{ctx.session_id}] Utterance boundary "
            f"({len(frames)} samples, {len(frames) / AudioPreprocessor.TARGET_SR:.2f}s). "
            f"Dispatching inference."
        )

        threading.Thread(
            target=self._run_utterance,
            args=(ctx, utterance_bytes),
            daemon=True,
        ).start()

    def _handle_control_event(self, ctx: SessionContext, vad: VADDetector, control: agent_pb2.ControlSignal) -> None:
        if control.profile.system_prompt:
            ctx.system_prompt = control.profile.system_prompt
            logger.info(f"[{ctx.session_id}] Profile received — agent: '{control.profile.agent_name}'")

        if control.type != agent_pb2.ControlSignal.END_OF_UTTERANCE:
            return

        if not ctx.utterance_done_event.is_set():
            ctx.utterance_done_event.wait()

        self._dispatch_utterance(ctx, vad)

    def _handle_audio_event(self, ctx: SessionContext, vad: VADDetector, preprocessor: AudioPreprocessor, audio: agent_pb2.AudioChunk) -> None:
        for frame in preprocessor.push(audio.data):
            command = vad.process_frames(frame)

            if command == VADCommand.START_SPEECH:
                logger.info(f"[{ctx.session_id}] VAD: speech started.")
                return

            if command == VADCommand.END_OF_UTTERANCE:
                if not ctx.utterance_done_event.is_set():
                    logger.warning(f"[{ctx.session_id}] VAD boundary while utterance in progress. Waiting.")
                    ctx.utterance_done_event.wait()

                self._dispatch_utterance(ctx, vad)

    def _read_pump(self, request_iterator, ctx: SessionContext) -> None:
        vad = VADDetector(VAD_MODEL_PATH)
        preprocessor = AudioPreprocessor(source_sr=SOURCE_SAMPLE_RATE)
        ctx.utterance_done_event.set()

        try:
            for event in request_iterator:
                ctx.session_id = event.session_id

                if event.HasField("control"):
                    self._handle_control_event(ctx, vad, event.control)
                elif event.HasField("audio"):
                    self._handle_audio_event(ctx, vad, preprocessor, event.audio)

        except grpc.RpcError as e:
            logger.error(f"[{ctx.session_id}] gRPC stream error in read pump: {e}", exc_info=True)
        finally:
            logger.info(f"[{ctx.session_id}] Inbound stream closed. Signaling shutdown.")
            ctx.outbound_queue.put(_SHUTDOWN)

    def StreamEvents(self, request_iterator, context) -> None:
        logger.info("Incoming gRPC stream connected.")

        ctx = SessionContext(
            session_id="unknown",
            outbound_queue=queue.Queue(),
            grpc_context=context,
        )

        pump_thread = threading.Thread(
            target=self._read_pump,
            args=(request_iterator, ctx),
            daemon=True,
        )
        pump_thread.start()

        while True:
            event = ctx.outbound_queue.get()
            if event is _SHUTDOWN:
                break
            yield event

        logger.info(f"[{ctx.session_id}] StreamEvents generator exiting.")


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=20),
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ],
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
