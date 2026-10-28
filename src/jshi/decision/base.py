"""Provider-neutral System One values. Missing confidence is not probability."""
from dataclasses import dataclass, field
from typing import Mapping, Protocol
import math


@dataclass(frozen=True)
class DecisionRequest:
    purpose: str
    state: Mapping
    questions: Mapping
    revision: int = 0


@dataclass(frozen=True)
class DecisionResult:
    status: str
    answers: Mapping = field(default_factory=dict)
    provider: str = ''
    model: str = ''
    elapsed_ms: float = 0
    server_ms: float | None = None
    request_id: str = ''
    usage: Mapping = field(default_factory=dict)
    reason: str = ''

    def choice(self, question, threshold=.85):
        answer = self.answers.get(question, {})
        confidence = answer.get('confidence')
        if self.status != 'success' or confidence is None or confidence < threshold:
            return 'uncertain'
        return answer.get('choice', 'uncertain')


class DecisionPort(Protocol):
    async def decide(self, request: DecisionRequest, *, deadline: float) -> DecisionResult: ...


def number(value, low=0, high=1):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_answers(questions, answers):
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError('answer_keys')
    for key, q in questions.items():
        a = answers[key]
        if not isinstance(a, dict) or a.get('type') != q['type']:
            raise ValueError('answer_type')
        if q['type'] == 'noul':
            if not number(a.get('noul')):
                raise ValueError('noul')
            continue
        options = set(q['criteria']) if q['type'] == 'choice' else {str(i) for i in range(len(q['criteria']))}
        probabilities = a.get('probabilities')
        if (not isinstance(probabilities, dict) or set(probabilities) != options
                or not all(number(p) for p in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > .02):
            raise ValueError('probabilities')
        if a.get('confidence') is not None and not number(a['confidence']):
            raise ValueError('confidence')
        if q['type'] == 'choice' and a.get('choice') not in options:
            raise ValueError('choice')
        if q['type'] == 'score' and not number(a.get('score'), 0, len(options)-1):
            raise ValueError('score')
    return answers
