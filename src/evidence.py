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
        if re.search(r"copyright", line, re.I):
            continue
        if re.fullmatch(r"\d+\.\d+", line):
            continue
        useful_lines.append(line)

    if not useful_lines:
        return -1
    if re.match(
        r"^(outline|agenda|contents|table of contents|references|thank you|questions?)(?:\b|\?)",
        useful_lines[0],
        re.I,
    ):
        return -1

    score = sum(len(re.findall(r"\w+", line)) for line in useful_lines)
    return min(score, 80) if score >= 3 else -1
