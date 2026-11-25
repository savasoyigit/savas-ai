from pathlib import Path
import shutil

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(
    title="Savas AI - Obsidian Agent",
    version="0.2.0",
)


VAULT = Path("/mnt/vault").resolve()


class NoteRequest(BaseModel):
    path: str
    content: str


class MoveNoteRequest(BaseModel):
    source: str
    destination: str


def safe_path(relative_path: str) -> Path:
    path = (VAULT / relative_path).resolve()

    try:
        path.relative_to(VAULT)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Unsafe path",
        )

    return path


def validate_markdown_path(path: Path):
    if path.suffix.lower() != ".md":
        raise HTTPException(
            status_code=400,
            detail="Only Markdown notes are allowed",
        )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "obsidian-agent",
        "vault": str(VAULT),
    }


@app.get("/folders")
def list_folders():
    folders = []

    for path in VAULT.rglob("*"):
        if path.is_dir():
            folders.append(
                str(path.relative_to(VAULT))
            )

    return {
        "folders": sorted(folders),
    }


@app.get("/folder")
def list_folder(path: str = ""):
    folder_path = safe_path(path)

    if not folder_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Folder not found",
        )

    if not folder_path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Path is not a folder",
        )

    items = []

    for item in sorted(
        folder_path.iterdir(),
        key=lambda p: (
            not p.is_dir(),
            p.name.lower(),
        ),
    ):
        items.append(
            {
                "name": item.name,
                "type": (
                    "folder"
                    if item.is_dir()
                    else "file"
                ),
                "path": str(
                    item.relative_to(VAULT)
                ),
            }
        )

    return {
        "path": path,
        "items": items,
    }


@app.get("/notes")
def list_notes():
    notes = []

    for path in VAULT.rglob("*.md"):
        notes.append(
            str(path.relative_to(VAULT))
        )

    return {
        "notes": sorted(notes),
    }


@app.get("/search")
def search_notes(q: str):
    query = q.strip().lower()

    if not query:
        raise HTTPException(
            status_code=400,
            detail="Search query cannot be empty",
        )

    results = []

    for path in VAULT.rglob("*.md"):
        try:
            content = path.read_text(
                encoding="utf-8"
            )
        except (
            UnicodeDecodeError,
            OSError,
        ):
            continue

        if (
            query in content.lower()
            or query in path.name.lower()
        ):
            results.append(
                {
                    "path": str(
                        path.relative_to(VAULT)
                    ),
                    "name": path.name,
                }
            )

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }


@app.get("/note")
def read_note(path: str):
    note_path = safe_path(path)

    validate_markdown_path(note_path)

    if not note_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Note not found",
        )

    if not note_path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Path is not a file",
        )

    return {
        "path": str(
            note_path.relative_to(VAULT)
        ),
        "content": note_path.read_text(
            encoding="utf-8"
        ),
    }


@app.post("/note")
def create_note(request: NoteRequest):
    note_path = safe_path(request.path)

    validate_markdown_path(note_path)

    if note_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Note already exists",
        )

    note_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    note_path.write_text(
        request.content,
        encoding="utf-8",
    )

    return {
        "status": "created",
        "path": str(
            note_path.relative_to(VAULT)
        ),
    }


@app.put("/note")
def update_note(request: NoteRequest):
    note_path = safe_path(request.path)

    validate_markdown_path(note_path)

    if not note_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Note not found",
        )

    if not note_path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Path is not a file",
        )

    note_path.write_text(
        request.content,
        encoding="utf-8",
    )

    return {
        "status": "updated",
        "path": str(
            note_path.relative_to(VAULT)
        ),
    }


@app.post("/move")
def move_note(request: MoveNoteRequest):
    source_path = safe_path(
        request.source
    )

    destination_path = safe_path(
        request.destination
    )

    validate_markdown_path(source_path)
    validate_markdown_path(destination_path)

    if not source_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Source note not found",
        )

    if not source_path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Source is not a file",
        )

    if destination_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Destination already exists",
        )

    destination_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(source_path),
        str(destination_path),
    )

    return {
        "status": "moved",
        "source": str(
            source_path.relative_to(VAULT)
        ),
        "destination": str(
            destination_path.relative_to(VAULT)
        ),
    }
