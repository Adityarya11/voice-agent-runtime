import numpy as np
import scipy.signal


class AudioPreprocessor:
    TARGET_SR = 16000
    FRAME_SIZE = 512
    RESAMPLE_CHUNK_MS = 100

    def __init__(self, source_sr: int):
        self._source_sr = source_sr
        gcd = np.gcd(self.TARGET_SR, source_sr)
        self._up = self.TARGET_SR // gcd
        self._down = source_sr // gcd

        self._raw_buffer = np.array([], dtype=np.float32)
        self._resample_chunk_samples = max(
            1, int(source_sr * self.RESAMPLE_CHUNK_MS / 1000)
        )

        self._remainder = np.array([], dtype=np.float32)

    def push(self, raw_bytes: bytes):
        incoming = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._raw_buffer = np.concatenate([self._raw_buffer, incoming])

        while len(self._raw_buffer) >= self._resample_chunk_samples:
            block = self._raw_buffer[: self._resample_chunk_samples]
            self._raw_buffer = self._raw_buffer[self._resample_chunk_samples :]

            if self._source_sr != self.TARGET_SR:
                resampled = scipy.signal.resample_poly(
                    block, self._up, self._down
                ).astype(np.float32)
            else:
                resampled = block

            combined = np.concatenate([self._remainder, resampled])
            num_frames = len(combined) // self.FRAME_SIZE

            for i in range(num_frames):
                yield combined[i * self.FRAME_SIZE : (i + 1) * self.FRAME_SIZE]

            self._remainder = combined[num_frames * self.FRAME_SIZE :]
