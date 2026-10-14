"""Independent analyzers contribute evidence; absence and failure stay explicit."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from time import time


@dataclass(frozen=True)
class VisualEvidence:
    module: str
    frame: str
    observed_at: float
    status: str
    result: object = None
    completed_at: float = 0
    version: str = "1"
    error: str = ""


class EvidenceModules:
    def __init__(self, modules=()):
        self.modules = dict(modules)

    def analyze(self, image, frame, captured, *, subject):
        def run(name, module):
            try:
                import inspect
                parameters = inspect.signature(module.analyze).parameters
                kwargs = {'subject':subject}
                if 'observed_at' in parameters: kwargs['observed_at'] = captured
                if 'frame' in parameters: kwargs['frame'] = frame
                value = module.analyze(image, **kwargs)
                status = value.pop('_status', 'completed') if isinstance(value, dict) else 'completed'
                return asdict(VisualEvidence(name, frame, captured, status, value, time()))
            except Exception as exc:
                return asdict(VisualEvidence(name, frame, captured, 'failed', None, time(), error=type(exc).__name__))
        if not self.modules:
            return []
        if len(self.modules) == 1:
            return [run(*next(iter(self.modules.items())))]
        with ThreadPoolExecutor(max_workers=min(4, len(self.modules))) as pool:
            jobs = [pool.submit(run, name, module) for name, module in self.modules.items()]
            return [job.result() for job in jobs]


def environment_experience_interface():
    # Reserved, deliberately no environment matching or experience retrieval.
    return {'status': 'not_implemented', 'references': []}
