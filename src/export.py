import json
import html
import math
import re
import sys
from pathlib import Path

import pymupdf as fitz

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")

    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    question_set = json.loads(
        (run_dir / "questions/curated_set.json").read_text(encoding="utf-8")
    )
    verification = json.loads(
        (run_dir / "questions/verification.json").read_text(encoding="utf-8")
    )
    repair_log_path = run_dir / "questions/repair_log.json"
    repair_log = (
        json.loads(repair_log_path.read_text(encoding="utf-8"))
        if repair_log_path.exists()
        else {"repairs": []}
    )

    # Index repair details by question number for the exported guide.
    repairs_by_index = {
        item["question_index"]: item
        for item in repair_log.get("repairs", [])
        if item.get("status") == "repaired" or (item.get("repaired_question") and item.get("repaired_question") != item.get("original_question"))
    }

    verifier_meta = verification.get("verifier", {})
    verifier_model = verifier_meta.get("model", "unknown")
    verifier_provider = verifier_meta.get("provider", "local")

    results_by_question = {
        (item["source_section"], item["question"]): item["verification"]
        for item in verification["results"]
    }

    lines = [
        "# Lecture Slides Study Guide",
        "",
        f"**Sections:** {len(sections)}  ",
        f"**Questions:** {len(question_set['questions'])}  ",
        f"**Citation checker:** {verifier_model} ({verifier_provider})  ",
    ]

    if repairs_by_index:
        lines.append(f"**Automatically Repaired Questions:** {len(repairs_by_index)}  ")

    lines.extend([
        "",
        "## Section Summaries",
        "",
    ])

    for section in sections:
        number = section["section"]
        summary_path = run_dir / "section_summaries" / f"section_{number:02}.json"

        if not summary_path.exists():
            raise SystemExit(f"Missing summary: {summary_path}")

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        lines.extend([
            f"### Section {number}: {summary['section_title']}",
            "",
        ])

        for point in summary["key_points"]:
            slide_list = ", ".join(str(slide) for slide in point["source_slides"])
            lines.append(f"- {point['text']} *(Slides {slide_list})*")

        lines.append("")

    lines.extend(["## Exam Questions", ""])
    flagged = []

    for number, question in enumerate(question_set["questions"], start=1):
        key = (question["source_section"], question["question"])
        check = results_by_question.get(key)

        if check is None:
            raise SystemExit(f"Missing verifier result for question {number}.")

        status = check["supported"]
        repair_info = repairs_by_index.get(number)
        repair_badge = " *(Repaired)*" if repair_info else ""

        lines.extend([
            f"### Question {number}{repair_badge}",
            "",
            question["question"],
            "",
            f"**Answer:** {question['answer']}",
            "",
            f"**Type:** {question['type']}  ",
            f"**Source:** Slide {question['source_slide']}  ",
            f"**Citation check:** {status} — {check['explanation']}",
        ])
        if repair_info:
            lines.append(f"  - **Original Question:** *\"{repair_info['original_question']}\"*")
            lines.append(f"  - **Original Answer:** *\"{repair_info['original_answer']}\"*")
        for claim in check.get("claims", []):
            lines.append(
                f"  - **{claim['source'].capitalize()} claim ({claim['status']}):** "
                f"{claim['claim']} Evidence: {claim['evidence']}"
            )
        lines.append("")

        if status != "yes":
            flagged.append((number, question, check))

    lines.extend(["## Items Needing Review", ""])

    if flagged:
        for number, question, check in flagged:
            lines.append(
                f"- Question {number} (Slide {question['source_slide']}): "
                f"{check['supported']} — {check['explanation']}"
            )
    else:
        lines.append("No questions were flagged by the citation checker.")

    output_path = run_dir / "study_guide.md"
    markdown = "\n".join(lines) + "\n"
    output_path.write_text(markdown, encoding="utf-8")


    def inline_html(text: str) -> str:
        text = html.escape(text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)
        return text


    def markdown_blocks(source: str) -> list[tuple[str, str]]:
        blocks = []
        for line in source.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("### "):
                blocks.append(("h2", line[4:]))
            elif line.startswith("## "):
                blocks.append(("h1", line[3:]))
            elif line.startswith("# "):
                blocks.append(("title", line[2:]))
            elif line.startswith("- "):
                blocks.append(("bullet", line[2:]))
            elif line.startswith("**"):
                blocks.append(("meta", line))
            else:
                blocks.append(("body", line))
        return blocks


    def create_pdf(source: str, destination: Path) -> None:
        page_width, page_height = fitz.paper_rect("letter").width, fitz.paper_rect("letter").height
        margin = 52
        bottom = 52
        document = fitz.open()
        page = document.new_page(width=page_width, height=page_height)
        y = margin
        styles = {
            "title": (20, "#17365d", 20),
            "h1": (15, "#17365d", 16),
            "h2": (11, "#245b87", 11),
            "meta": (9, "#444444", 4),
            "bullet": (9, "#222222", 4),
            "body": (9, "#222222", 5),
        }
        for kind, text in markdown_blocks(source):
            size, color, gap = styles[kind]
            prefix = "• " if kind == "bullet" else ""
            tag = "h1" if kind in {"title", "h1", "h2"} else "p"
            content = f"<{tag}>{prefix}{inline_html(text)}</{tag}>"
            css = f"body {{ font-family: sans-serif; font-size: {size}pt; color: {color}; }} {tag} {{ font-size: {size}pt; line-height: 1.2; margin: 0; padding: 0; }} code {{ font-family: monospace; }}"
            chars_per_line = 38 if kind in {"title", "h1", "h2"} else 82
            estimated_lines = max(1, math.ceil(len(prefix + text) / chars_per_line))
            block_height = estimated_lines * size * 1.55 + gap + 8
            if y + block_height > page_height - bottom:
                page = document.new_page(width=page_width, height=page_height)
                y = margin
            rect = fitz.Rect(margin, y, page_width - margin, y + block_height)
            spare, scale = page.insert_htmlbox(rect, content, css=css, scale_low=1.0)
            if scale == 0:
                block_height *= 1.5
                if y + block_height > page_height - bottom:
                    page = document.new_page(width=page_width, height=page_height)
                    y = margin
                rect = fitz.Rect(margin, y, page_width - margin, y + block_height)
                spare, scale = page.insert_htmlbox(rect, content, css=css, scale_low=1.0)
            y += block_height

        for page_number, pdf_page in enumerate(document, start=1):
            pdf_page.insert_text(
                fitz.Point(margin, page_height - 25),
                f"Lecture Slides Study Guide  |  {page_number}",
                fontsize=8,
                color=(0.4, 0.4, 0.4),
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination, garbage=4, deflate=True, clean=True)
        document.close()


    pdf_path = run_dir / "study_guide.pdf"
    create_pdf(markdown, pdf_path)
    print(f"Saved study guide to {output_path}")
    print(f"Saved PDF study guide to {pdf_path}")


if __name__ == '__main__':
    main()
