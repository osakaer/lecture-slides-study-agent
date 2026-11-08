import json
import re
import sys
from pathlib import Path

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
    slides_path = run_dir / "extracted.json"
    sections_path = run_dir / "sections.json"

    min_size = int(arguments[2]) if len(arguments) > 2 else 4
    max_size = int(arguments[3]) if len(arguments) > 3 else 7

    if not slides_path.exists():
        raise SystemExit(f"Extracted slides not found: {slides_path}")

    slides = json.loads(slides_path.read_text(encoding="utf-8"))


    def extract_title(slide: dict) -> str:
        """Extract a clean, topical title for a slide by skipping boilerplate headers."""
        lines = [l.strip() for l in slide.get("text", "").splitlines() if l.strip()]
        cleaned = []
        for line in lines:
            # Skip generic metadata, textbook headers, course codes, copyright
            if re.search(
                r"^(CS\s*\d+|database system concepts|copyright|©|silberschatz|korth|sudarshan|\d+\.\d+|\d+)$",
                line,
                re.I,
            ):
                continue
            if re.search(r"silberschatz,\s*korth|see\s+www\.db-book\.com", line, re.I):
                continue
            cleaned.append(line)
        return cleaned[0] if cleaned else "Untitled Slide"


    def is_major_boundary(title: str, prev_title: str) -> bool:
        """Check if the slide marks a natural section or topic change."""
        # Continuations should never be boundaries
        if re.search(r"\(cont|\bcontinued\b|part\s*2\b", title, re.I):
            return False
        # Explicit topic or chapter headings
        if re.search(
            r"^(topic\s*\d+|chapter\s*\d+|part\s*\d+|section\s*\d+|att-\d+|content\b|outline\b|agenda\b)",
            title,
            re.I,
        ):
            return True
        # Major architectural or conceptual transitions
        if re.search(
            r"^(solution:|introduction to|overview\b|review\b|applications?\b|standards?\b|hash functions\b|block ciphers\b|public key\b|relational\b)",
            title,
            re.I,
        ):
            return True
        # Significant normalized prefix change
        t1_norm = re.sub(r"[^a-z0-9]", "", title.lower())[:8]
        t2_norm = re.sub(r"[^a-z0-9]", "", prev_title.lower())[:8]
        return t1_norm != t2_norm


    def create_title_aware_sections(
        slides: list[dict], min_s: int = 4, max_s: int = 7
    ) -> list[dict]:
        n = len(slides)
        if n <= max_s:
            first = slides[0]["slide"]
            last = slides[-1]["slide"]
            clean_name = re.sub(r"^(Topic\s*\d+|ATT-\d+):\s*", "", extract_title(slides[0]))
            return [
                {
                    "section": 1,
                    "title": f"{clean_name} (Slides {first}–{last})",
                    "start_slide": first,
                    "end_slide": last,
                    "slide_numbers": [s["slide"] for s in slides],
                }
            ]

        splits = [0]
        i = min_s
        while i < n:
            current_len = i - splits[-1]
            remaining = n - i

            if current_len >= max_s:
                splits.append(i)
                i += min_s
                continue

            title = extract_title(slides[i])
            prev_title = extract_title(slides[i - 1])

            # Only split if boundary detected and remaining slides can form a valid section
            if is_major_boundary(title, prev_title) and remaining >= min_s:
                splits.append(i)
                i += min_s
            else:
                i += 1

        splits.append(n)
        sections = []
        for idx in range(len(splits) - 1):
            start_idx = splits[idx]
            end_idx = splits[idx + 1]
            sec_slides = slides[start_idx:end_idx]
            s_nums = [s["slide"] for s in sec_slides]
            first_title = extract_title(sec_slides[0])
            clean_name = re.sub(r"^(Topic\s*\d+|ATT-\d+):\s*", "", first_title)
            sections.append(
                {
                    "section": idx + 1,
                    "title": f"{clean_name} (Slides {s_nums[0]}–{s_nums[-1]})",
                    "start_slide": s_nums[0],
                    "end_slide": s_nums[-1],
                    "slide_numbers": s_nums,
                }
            )
        return sections


    sections = create_title_aware_sections(slides, min_size, max_size)

    sections_path.write_text(
        json.dumps(sections, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Created {len(sections)} title-aware sections from {len(slides)} slides.")
    for section in sections:
        print(
            f"Section {section['section']}: "
            f"slides {section['start_slide']}–{section['end_slide']} "
            f"({len(section['slide_numbers'])} slides) | {section['title']}"
        )
    print(f"Saved section index to {sections_path}.")


if __name__ == '__main__':
    main()
