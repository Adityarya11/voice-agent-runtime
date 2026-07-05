import numpy as np
import scipy.signal


class AudioPreprocessor:
    TARGET_SR = 16000
    FRAME_SIZE = 512

    def __init__(self, source_sr: int):
        self._source_sr = source_sr
        gcd = np.gcd(self.TARGET_SR, source_sr)
        self._up = self.TARGET_SR // gcd
        self._down = source_sr // gcd
        self._remainder = np.array([], dtype=np.float32)

    def push(self, raw_bytes: bytes):
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        if self._source_sr != self.TARGET_SR:
            samples = scipy.signal.resample_poly(
                samples, self._up, self._down
            ).astype(np.float32)

        combined = np.concatenate([self._remainder, samples])
        num_frames = len(combined) // self.FRAME_SIZE

        for i in range(num_frames):
            yield combined[i * self.FRAME_SIZE:(i + 1) * self.FRAME_SIZE]

        self._remainder = combined[num_frames * self.FRAME_SIZE:]