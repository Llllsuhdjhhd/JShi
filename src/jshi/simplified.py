"""Simplified working prose at ingress/write boundaries, never per recall candidate."""
from functools import lru_cache
import re

LANGUAGE_RULE = '中文自然语言正文统一使用简体中文；标识符、引用代号、网址、代码及工具参数保持原样。'
_PROTECTED = re.compile(r'(```[\s\S]*?```|`[^`\n]*`|(?:https?://|wss?://|file://)[^\s<>"\u3000]+|(?:[A-Za-z]:[\\/]|\\\\)[^\s<>"，。；]+)')
_TEXT = frozenset('text content content_raw summary role_summary description reply waiting_reply reason why decision_reason change_reason name label aliases names title brief insight decoration location query input output observation question answer fact claim statement explanation unsaid intent need stance narrative identity_summary current_stance l1_mention l2_interaction l3_decision scene topics topic environment participants relationships facts details basis portrait verbal embodied action meaning interpretation conclusion instruction feedback suggestion'.split())
_MAPS = frozenset('summaries levels refinement_object_levels role_snapshot content_fragments logical_gaps buffer_items alternatives revised_claims body'.split())
_MACHINE = frozenset('id key code url path reference source_ids status mode route type channel format model model_name schema json_schema arguments parameters tool_input raw raw_text original original_text version hash source_hash signature event_ids object_ids target_ids evidence_refs process_refs pending_refs decision_refs'.split())

@lru_cache(maxsize=1)
def _converter():
    from opencc import OpenCC
    return OpenCC('t2s')

@lru_cache(maxsize=4096)
def simplify_text(text):
    if not text or not any('\u3400' <= c <= '\u9fff' for c in text):
        return text
    return ''.join(piece if index % 2 else _converter().convert(piece)
                   for index, piece in enumerate(_PROTECTED.split(text)))

def simplify_data(value, *, prose=False):
    """Convert owned prose fields; never dictionary keys or protocol identifiers."""
    if isinstance(value, str):
        return simplify_text(value) if prose else value
    if isinstance(value, dict):
        return {key: item if key in _MACHINE or isinstance(key,str) and key.endswith(('_id', '_ids', '_ref', '_refs', '_hash', '_key', '_keys', '_url', '_path', '_code'))
                else simplify_data(item, prose=prose or key in _TEXT or key in _MAPS)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        result=[simplify_data(item, prose=prose) for item in value]
        return tuple(result) if isinstance(value, tuple) else result
    return value
