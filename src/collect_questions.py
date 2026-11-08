import json
import re
import sys
from pathlib import Path

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')


    run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
    question_dir = run_dir / "questions"

    if not question_dir.exists():
        raise SystemExit(f"Question folder not found: {question_dir}")

    questions = []
    for question_path in sorted(question_dir.glob("section_*.json")):
        match = re.fullmatch(r"section_(\d+)(?:_q(\d+))?\.json", question_path.name)
        if match is None:
            continue

        section_number = int(match.group(1))
        question_number = int(match.group(2)) if match.group(2) else 1
        question = json.loads(question_path.read_text(encoding="utf-8"))
        questions.append({
            "source_section": section_number,
            "candidate_number": question_number,
            "question": question["question"],
            "answer": question["answer"],
            "type": question["type"],
            "source_slide": question["source_slide"],
            "include_slide_image": True,
        })

    if not questions:
        raise SystemExit(f"No section question files found in {question_dir}")

    question_set = {
        "title": f"{run_dir.name} curated questions",
        "question_count": len(questions),
        "questions": questions,
    }
    output_path = question_dir / "curated_set.json"
    output_path.write_text(
        json.dumps(question_set, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Collected {len(questions)} questions in {output_path}.")
    print("Question candidates collected; automatic verification is next.")


if __name__ == '__main__':
    main()
