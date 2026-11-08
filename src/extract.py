import json
import sys
from pathlib import Path

import pymupdf

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    pdf_path = Path(arguments[1]) if len(arguments) > 1 else Path("data/sample.pdf")

    if not pdf_path.exists():
        raise SystemExit(f"File not found: {pdf_path}")

    deck_dir = Path(arguments[2]) if len(arguments) > 2 else Path("data/runs") / pdf_path.stem
    deck_dir.mkdir(parents=True, exist_ok=True)
    image_dir = deck_dir / "rendered"
    image_dir.mkdir(parents=True, exist_ok=True)

    slides = []

    with pymupdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages(), start=1):
            text = page.get_text("text").strip()
            image_path = image_dir / f"slide_{page_number:03}.png"

            page.get_pixmap(dpi=150).save(image_path)

            slides.append({
                "slide": page_number,
                "text": text,
                "image":str(image_path).replace("\\", "/")
            })
            if len(text) < 20:
                print(f"Note: slide {page_number} has little or no extractable text.")

    Path(deck_dir / "extracted.json").write_text(
        json.dumps(slides, ensure_ascii=False, indent=2),
        encoding="utf-8"
        )

    print(f"Processed {len(slides)} slides from {pdf_path}.")
    print(f"Slide images saved in {image_dir}.")
    print("\n--- Slides 1–5: extracted text ---")

    for slide in slides[:5]:
        print(f"\n[Slide {slide['slide']}]")
        print(slide["text"] or "(No extractable text)")
        print(f"Image: {slide['image']}")


if __name__ == '__main__':
    main()
