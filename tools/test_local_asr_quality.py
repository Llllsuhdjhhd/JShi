"""Compare real local models on a repeatable synthetic English sample.

The generated voice is NOT the user's voice and cannot validate LUX identity.
No microphone, online calls, reference-bank updates or new downloads.
"""
from io import BytesIO
import json
from pathlib import Path
from time import perf_counter
import numpy as np
from scipy.signal import resample_poly
from math import gcd
import sherpa_onnx
from jshi.voice.local_tts import build_local_tts
from jshi.voice.config import VoiceConfig
from jshi.voice.local import REFINER_NAME
from jshi.voice.asr_text import clean_asr_text


def main(output):
    root=Path('.jshi/voice_models')
    expected='Hello, can you hear me? I am speaking English.'
    tts=build_local_tts(Path('.jshi'),VoiceConfig(api_key=''))
    audio=tts.engine.generate(expected,sid=0,speed=1.)
    divisor=gcd(audio.sample_rate,16000)
    samples=resample_poly(np.asarray(audio.samples,dtype='float32'),16000//divisor,audio.sample_rate//divisor).astype('float32')
    rows=[]
    for language in ('zh','auto'):
        recognizer=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(root/REFINER_NAME/'model.int8.onnx'),tokens=str(root/REFINER_NAME/'tokens.txt'),num_threads=2,provider='cpu',language=language,use_itn=True)
        for mode in ('whole','one_second_segments'):
            start=perf_counter()
            parts=[samples] if mode=='whole' else [samples[i:i+16000] for i in range(0,len(samples),16000)]
            texts=[]
            for part in parts:
                stream=recognizer.create_stream();stream.accept_waveform(16000,part);recognizer.decode_stream(stream)
                texts.append(clean_asr_text(stream.result.text))
            rows.append({'language':language,'mode':mode,'segments':len(parts),'text':' '.join(texts),'ms':round((perf_counter()-start)*1000)})
    report={'sample':'local TTS synthetic English, not user audio','expected':expected,'seconds':len(samples)/16000,'results':rows}
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);main(p.parse_args().output)
