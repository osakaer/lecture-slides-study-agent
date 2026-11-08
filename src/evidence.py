"""Shared heuristics for choosing slides with usable study evidence."""

import re


def evidence_score(text: str) -> int:
    """Score slide text, returning -1 for end matter or near-empty OCR."""
    if re.search(r"\bend of (chapter|lecture)\b", text, re.I):
        return -1

    useful_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r"copyright|database system concepts|7th edition", line, re.I):
            continue
        if re.fullmatch(r"\d+\.\d+", line):
            continue
        useful_lines.append(line)

    score = sum(len(re.findall(r"\w+", line)) for line in useful_lines)
    return score if score >= 3 else -1
