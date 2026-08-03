import io
import logging
import os
import queue
import signal
import sys
import threading
import wave
from collections.abc import Generator
from concurrent import futures
from dataclasses import dataclass, field

import grpc
import numpy as np
import scipy.signal

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
_UTTERANCE_STOP = object()
VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
DEFAULT_SOURCE_SAMPLE_RATE = 44100
TTS_OUTPUT_SAMPLE_RATE = 8000


def wav_to_pcm16_mono(wav_bytes: bytes, target_sr: int = TTS_OUTPUT_SAMPLE_RATE) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_in:
        source_sr = wav_in.getframerate()
        channels = wav_in.getnchannels()
        sample_width = wav_in.getsampwidth()
        frames = wav_in.readframes(wav_in.getnframes())

    if sample_width != 2:
        raise RuntimeError(f"Unsupported TTS sample width: {sample_width}")

    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)[:, 0]

    if source_sr != target_sr:
        gcd = np.gcd(source_sr, target_sr)
        samples_f32 = samples.astype(np.float32) / 32768.0
        samples_f32 = scipy.signal.resample_poly(
            samples_f32,
            target_sr // gcd,
            source_sr // gcd,
        ).astype(np.float32)
        samples = np.clip(samples_f32 * 32767.0, -32768, 32767).astype(np.int16)

    return samples.tobytes()


@dataclass
class SessionContext:
    session_id: str
    outbound_queue: queue.Queue
    grpc_context: grpc.ServicerContext
    utterance_queue: queue.Queue = field(default_factory=queue.Queue)
    utterance_done_event: threading.Event = field(default_factory=threading.Event)
    system_prompt: str | None = None
    conversation_history: list[dict[str, str]] = field(default_factory=list)


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
                logger.warning(
                    f"[{ctx.session_id}] Empty transcription, skipping inference."
                )
                return

            logger.info(f"[{ctx.session_id}] STT: '{user_text}'")

            assistant_parts: list[str] = []
            for sentence in self.llm.generate_stream(
                user_text,
                system_override=ctx.system_prompt,
                history=ctx.conversation_history,
            ):
                assistant_parts.append(sentence)
                if not ctx.grpc_context.is_active():
                    logger.warning(
                        f"[{ctx.session_id}] gRPC context cancelled mid-utterance. "
                        f"Stopping inference"
                    )
                    return

                wav_bytes = self.tts.synthesize(sentence)
                if not wav_bytes:
                    logger.warning(
                        f"[{ctx.session_id}] TTS returned empty bytes for: '{sentence}'"
                    )
                    continue

                pcm_bytes = wav_to_pcm16_mono(wav_bytes)

                for i in range(0, len(pcm_bytes), CHUNK_SIZE):
                    chunk = pcm_bytes[i : i + CHUNK_SIZE]
                    ctx.outbound_queue.put(
                        agent_pb2.Event(
                            session_id=ctx.session_id,
                            audio=agent_pb2.AudioChunk(data=chunk),
                        )
                    )

            if assistant_parts:
                ctx.conversation_history.extend(
                    [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": " ".join(assistant_parts)},
                    ]
                )
                ctx.conversation_history = ctx.conversation_history[-12:]

            logger.info(f"[{ctx.session_id}] Utterance response complete.")

        except grpc.RpcError as e:
            logger.error(
                f"[{ctx.session_id}] gRPC error during utterance: {e}", exc_info=True
            )
        except RuntimeError as e:
            logger.error(
                f"[{ctx.session_id}] Inference runtime failure: {e}", exc_info=True
            )
        finally:
            ctx.utterance_done_event.set()

    def _dispatch_utterance(self, ctx: SessionContext, vad: VADDetector) -> None:
        frames = vad.get_utterance_frames()
        if len(frames) == 0:
            logger.warning(
                f"[{ctx.session_id}] Boundary fired with empty VAD buffer, ignoring."
            )
            return

        utterance_bytes = frames_to_wav(frames, AudioPreprocessor.TARGET_SR)

        logger.info(
            f"[{ctx.session_id}] Utterance boundary "
            f"({len(frames)} samples, {len(frames) / AudioPreprocessor.TARGET_SR:.2f}s). "
            f"Queuing inference."
        )

        ctx.utterance_queue.put(utterance_bytes)

    def _utterance_worker(self, ctx: SessionContext) -> None:
        while True:
            utterance_bytes = ctx.utterance_queue.get()
            if utterance_bytes is _UTTERANCE_STOP:
                ctx.outbound_queue.put(_SHUTDOWN)
                return

            self._run_utterance(ctx, utterance_bytes)

    def _handle_control_event(
        self, ctx: SessionContext, vad: VADDetector, control: agent_pb2.ControlSignal
    ) -> None:
        if control.profile.system_prompt:
            ctx.system_prompt = control.profile.system_prompt
            logger.info(
                f"[{ctx.session_id}] Profile received — agent: '{control.profile.agent_name}'"
            )

        if control.type != agent_pb2.ControlSignal.END_OF_UTTERANCE:
            return

        self._dispatch_utterance(ctx, vad)

    def _handle_audio_event(
        self,
        ctx: SessionContext,
        vad: VADDetector,
        preprocessor: AudioPreprocessor,
        audio: agent_pb2.AudioChunk,
    ) -> None:
        for frame in preprocessor.push(audio.data):
            command = vad.process_frames(frame)

            if command == VADCommand.START_SPEECH:
                logger.info(f"[{ctx.session_id}] VAD: speech started.")
                continue

            if command == VADCommand.END_OF_UTTERANCE:
                self._dispatch_utterance(ctx, vad)

    def _read_pump(self, request_iterator, ctx: SessionContext) -> None:
        vad = VADDetector(VAD_MODEL_PATH, min_silence_duration=800)
        preprocessor: AudioPreprocessor | None = None
        ctx.utterance_done_event.set()

        try:
            for event in request_iterator:
                ctx.session_id = event.session_id

                if event.HasField("control"):
                    control = event.control
                    if (
                        control.type == agent_pb2.ControlSignal.START_SESSION
                        and preprocessor is None
                    ):
                        source_rate = (
                            control.source_sample_rate or DEFAULT_SOURCE_SAMPLE_RATE
                        )
                        preprocessor = AudioPreprocessor(source_sr=source_rate)
                        logger.info(
                            f"[{ctx.session_id}] Preprocessor initialized at source_sample_rate={source_rate}"
                        )
                    self._handle_control_event(ctx, vad, control)
                elif event.HasField("audio"):
                    if preprocessor is None:
                        logger.warning(
                            f"[{ctx.session_id}] Audio received before START_SESSION; defaulting sample rate."
                        )
                        preprocessor = AudioPreprocessor(
                            source_sr=DEFAULT_SOURCE_SAMPLE_RATE
                        )
                    self._handle_audio_event(ctx, vad, preprocessor, event.audio)

        except grpc.RpcError as e:
            logger.error(
                f"[{ctx.session_id}] gRPC stream error in read pump: {e}", exc_info=True
            )
        finally:
            logger.info(
                f"[{ctx.session_id}] Inbound stream closed. Signaling shutdown."
            )
            ctx.utterance_queue.put(_UTTERANCE_STOP)

    def StreamEvents(
        self, request_iterator, context
    ) -> Generator[agent_pb2.Event, None, None]:
        logger.info("Incoming gRPC stream connected.")

        ctx = SessionContext(
            session_id="unknown",
            outbound_queue=queue.Queue(),
            grpc_context=context,
        )

        worker_thread = threading.Thread(
            target=self._utterance_worker,
            args=(ctx,),
            daemon=True,
        )
        worker_thread.start()

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
