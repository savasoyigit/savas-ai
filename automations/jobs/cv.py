"""
The CV for scoring (J-4) and packages (J-5): the PDF in ~/AI/Kariyer/ ->
text (pdftotext, already on the host) -> proposed as the Obsidian note
Kariyer/CV.md; once approved the note is the source and the user edits it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import lib
from jobs import profile

PDF_DIR = lib.ROOT / "Kariyer"
NOTE = "Kariyer/CV.md"
MAX_CHARS = 12000


def pdf_text(pdf: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=60, check=True)
    except FileNotFoundError as exc:
        raise lib.AutomationError("pdftotext yok (poppler-utils)") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise lib.AutomationError(f"pdftotext {pdf.name} okuyamadı") from exc
    text = out.stdout.decode("utf-8", "replace").replace("\f", "\n")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def newest_pdf() -> Path:
    pdfs = sorted(PDF_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    if not pdfs:
        raise lib.AutomationError(f"{PDF_DIR} içinde PDF yok")
    return pdfs[-1]


def note_text(pdf: Path, text: str, day: str) -> str:
    return (f"# CV\n\nKaynak: `{pdf.name}` (pdftotext, {day}). İlan puanlama (J-4) ve başvuru paketleri (J-5) "
            "bu notu kullanır; yanlış ya da eksik okunan yerleri burada düzelt. Başvuru paketlerinde yalnız "
            "buradaki bilgiler kullanılır.\n\n---\n\n" + text + "\n")


def load() -> str | None:
    """The approved note's text, or None when there is no note yet."""
    note = profile.VAULT / NOTE
    if not note.is_file():
        return None
    return note.read_text(encoding="utf-8")[:MAX_CHARS]
