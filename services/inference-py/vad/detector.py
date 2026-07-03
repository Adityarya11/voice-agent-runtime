import enum 
import os

import onnxruntime as ort 
import numpy as np


class VADState(enum.Enum):
    SILENCE = "SILENCE"
    SPEECH_STARTING = "SPEECH_STARTING"
    SPEECH = "SPEECH"
    SPEECH_ENDING = "SPEECH_ENDING"

class VADCommand(enum.Enum):
    NONE = "NONE"
    START_SPEECH = "START_SPEECH"
    END_OF_UTTERANCE = "END_OF_UTTERANCE"


class VADDetector: 

    FRAME_SIZE = 512
    SAMPLE_RATE = 16000

    FRAME_DURATION_MS = FRAME_SIZE / SAMPLE_RATE * 1000

    def __init__(self, model_path: str, threshold: float=0.5, min_speech_duration: float=250, min_silence_duration: float=500):

        if not os.path.exists(model_path): 
            raise FileNotFoundError(f"Silero VAD model not found at: {model_path}")

        self._session = ort.InferenceSession(model_path)
        self._sr = np.array(self.SAMPLE_RATE, dtype=np.int64)

        self.threshold = threshold
        self._min_speech_frames = max(1, round(min_speech_duration / self.FRAME_DURATION_MS))
        self._min_silence_frames = max(1, round(min_silence_duration / self.FRAME_DURATION_MS))

        # Persists across utterances within a session; reset() is for
        # session boundaries only, not per-utterance.

        self._state = np.zeros((2, 1, 128), dtype=np.float32)

        self._vad_state = VADState.SILENCE
        self._speech_frame_count = 0
        self._silence_frame_count = 0

        self._pending_frames: list = []
        self._utterance_frames: list = []


    def process_frames(self, frame: np.ndarray) -> VADCommand: 
        if frame.shape != (self.FRAME_SIZE,): 
            raise ValueError(f"Expected frame of shape({self.FRAME_SIZE},), got {frame.shape}")

        score, self._state = self._infer(frame)
        is_speech = score > self.threshold

        if self._vad_state == VADState.SILENCE:
            return self._handle_silence(frame, is_speech)
        if self._vad_state == VADState.SPEECH_STARTING:
            return self._handle_speech_starting(frame, is_speech)
        if self._vad_state == VADState.SPEECH:
            return self._handle_speech(frame, is_speech)
        if self._vad_state == VADState.SPEECH_ENDING:
            return self._handle_speech_ending(frame, is_speech)
 
        raise RuntimeError(f"Unhandled VAD state: {self._vad_state}")

    def _handle_silence(self, frame: np.ndarray, is_speech: bool) -> VADCommand: 
        if is_speech: 
            self._vad_state = VADState.SPEECH_STARTING
            self._speech_frame_count = 1
            self._pending_frames = [frame]

        return VADCommand.NONE

    def _handle_speech_starting(self, frame: np.ndarray, is_speech: bool) -> VADCommand: 
        if not is_speech: 
            self._vad_state = VADState.SILENCE
            self._pending_frames = []
            return VADCommand.NONE

        self._speech_frame_count += 1
        self._pending_frames.append(frame)

        if self._speech_frame_count >= self._min_speech_frames: 
            self._vad_state = VADState.SPEECH
            self._utterance_frames = self._pending_frames
            self._pending_frames = []
            return VADCommand.START_SPEECH

        return VADCommand.NONE

    def _handle_speech(self, frame: np.ndarray, is_speech: bool) -> VADCommand:
        self._utterance_frames.append(frame)
        if not is_speech:
            self._vad_state = VADState.SPEECH_ENDING
            self._silence_frame_count = 1

        return VADCommand.NONE

        return VADCommand.NONE

    def _handle_speech_ending(self, frame: np.ndarray, is_speech: bool) -> VADCommand: 
        self._utterance_frames.append(frame)

        if is_speech:
            self._vad_state = VADState.SPEECH  # goes back to speaking state
            self._silence_frame_count = 0
            return VADCommand.NONE

        self._silence_frame_count += 1
        if self._silence_frame_count >= self._min_silence_frames:
            self._vad_state = VADState.SILENCE
            self._silence_frame_count = 0
            return VADCommand.END_OF_UTTERANCE

        return VADCommand.NONE

    def get_utterance_frames(self) -> np.ndarray: 

        if not self._utterance_frames: 
            return np.array([], dtype=np.float32)

        audio = np.concatenate(self._utterance_frames)
        self._utterance_frames = []

        return audio

    def reset(self) -> None:

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._vad_state = VADState.SILENCE
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._pending_frames = []
        self._utterance_frames = []

    def _infer(self, frame: np.ndarray) -> tuple:
        outputs = self._session.run(None, {
                "input": frame.reshape(1, self.FRAME_SIZE), 
                "sr": self._sr,
                "state": self._state,
            },
        )

        score = float(outputs[0][0][0])
        new_state = outputs[1]

        return score, new_state