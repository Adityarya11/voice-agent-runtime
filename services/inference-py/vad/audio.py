import io 
import wave

import numpy as np



def frames_to_wav(samples: np.ndarray, sample_rate:int) -> bytes: 

    pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    
    return buf.getvalue()