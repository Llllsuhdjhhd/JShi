"""Remove recognizer control tokens without rewriting recognized speech."""
import re


def clean_asr_text(text: str) -> str:
    return re.sub(r"<\|.*?\|>|<unk>", "", text).strip()
