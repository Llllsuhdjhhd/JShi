"""Local text-only turn detection. No prompt, remote call, identity or ASR."""
from typing import Protocol
import math

from .turns import TurnDecision

NAMO_REPO = 'videosdk-live/Namo-Turn-Detector-v1-Multilingual'
NAMO_REVISION = '59245aac1f0a0f170277ebecc32ddaea418083b4'
NAMO_FILES = ('model_quant.onnx','config.json','tokenizer.json',
              'tokenizer_config.json','special_tokens_map.json','LICENSE')


class SemanticTurnPolicy(Protocol):
    def analyze_text(self, transcripts) -> TurnDecision: ...


def text_view(transcripts):
    """Model-only ordered view. Never rewrite original rows or dedupe real repetition.

    Newlines keep fragment boundaries without inventing sentence punctuation or
    joining English words. The coordinator supplies a single speaker's rows.
    """
    return '\n'.join(row.text.strip() for row in sorted(transcripts,
        key=lambda row:(row.start_ms,row.end_ms)) if row.text.strip())


class NamoSemanticPolicy:
    name = 'namo'
    def __init__(self, root, *, threshold=.5, threads=2, max_tokens=512):
        import onnxruntime as ort
        from transformers import AutoTokenizer
        path = root/'model_quant.onnx'
        if not path.is_file():
            raise ValueError('Namo语义模型未准备，请运行 jshi voice-setup --semantic-turn；或设JSHI_VOICE_SEMANTIC_TURN=off')
        self.tokenizer = AutoTokenizer.from_pretrained(str(root), local_files_only=True,
                                                       trust_remote_code=False)
        self.tokenizer.truncation_side = 'left'
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                            providers=['CPUExecutionProvider'])
        self.threshold, self.max_tokens = threshold, max_tokens
        self.predict('你好。')  # Startup warmup, never the first live utterance.

    def predict(self, text):
        import numpy as np
        if not text.strip():
            return TurnDecision('unknown', reason='empty_text')
        inputs = self.tokenizer(text, truncation=True, max_length=self.max_tokens,
                                return_tensors='np')
        feed = {item.name:np.asarray(inputs[item.name],dtype='int64')
                for item in self.session.get_inputs()}
        logits = np.asarray(self.session.run(None, feed)[0]).reshape(-1)
        if len(logits) != 2 or not np.isfinite(logits).all():
            raise ValueError('Namo返回无效分类结果')
        exp = np.exp(logits - np.max(logits))
        probability = float((exp / exp.sum())[1])  # Official: 0=continue, 1=end.
        if not math.isfinite(probability):
            raise ValueError('Namo返回无效概率')
        return TurnDecision('complete' if probability >= self.threshold else 'incomplete',
                            probability, 'namo_text')

    def analyze_text(self, transcripts):
        return self.predict(text_view(transcripts))


SEMANTIC_POLICIES = {'off':lambda config,root:None,
    'namo':lambda config,root:NamoSemanticPolicy(root/'namo-turn',
                                                threshold=config.semantic_turn_threshold)}


def build_semantic_policy(config, root):
    return SEMANTIC_POLICIES[config.semantic_turn_strategy](config, root)
