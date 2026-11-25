"""
A package text (Markdown from the model) as a plain .docx (J-5 download).

Standard library only: a .docx is a zip of a few XML parts. What the
models write is covered: # headings, - / * / 1. list items, **bold**,
[links](url) (kept as their text), "---" rules (dropped), paragraphs.
Anything else stays as text.

cv_docx lays a structured CV (cv_layout) out in the user's own CV design,
with the photo (data/career/photo.jpg) top right.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="jpg" ContentType="image/jpeg"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>{image}
</Relationships>"""
IMAGE_REL = ('<Relationship Id="rIdPhoto" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
             'Target="media/photo.jpg"/>')

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {W}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>
<w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="tr-TR"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="60"/><w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" w:color="888888"/></w:pBdr></w:pPr>
<w:rPr><w:b/><w:caps/><w:sz w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="120" w:after="40"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListItem"><w:name w:val="List Item"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:after="40"/><w:ind w:left="360" w:hanging="220"/></w:pPr></w:style>
</w:styles>"""

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+(.*)$")
RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def runs(text: str) -> str:
    """Inline Markdown -> w:r elements (bold kept, links as their text, single * dropped)."""
    text = LINK.sub(r"\1", text)
    out, at = [], 0
    for match in BOLD.finditer(text):
        out.append((text[at:match.start()], False))
        out.append((match.group(1) or match.group(2), True))
        at = match.end()
    out.append((text[at:], False))
    xml = []
    for part, bold in out:
        part = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", part).replace("`", "")
        if part:
            props = "<w:rPr><w:b/></w:rPr>" if bold else ""
            xml.append(f'<w:r>{props}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(xml)


def paragraph(text: str, style: str | None = None, prefix: str = "") -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    lead = f'<w:r><w:t xml:space="preserve">{escape(prefix)}</w:t></w:r>' if prefix else ""
    return f"<w:p>{props}{lead}{runs(text)}</w:p>"


def body(markdown: str) -> str:
    paragraphs, pending, first = [], [], True

    def flush():
        if pending:
            paragraphs.append(paragraph(" ".join(pending)))
            pending.clear()

    for raw in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        hard_break = raw.endswith("  ")
        if not line.strip():
            flush()
            continue
        if RULE.match(line):
            flush()
            continue
        if heading := HEADING.match(line):
            flush()
            level = len(heading.group(1))
            style = "Title" if level == 1 and first else ("Heading1" if level <= 2 else "Heading2")
            paragraphs.append(paragraph(heading.group(2).strip(), style))
        elif bullet := BULLET.match(line):
            flush()
            paragraphs.append(paragraph(bullet.group(1).strip(), "ListItem", "•\t"))
        else:
            pending.append(line.strip())
            if hard_break:
                flush()
        first = False
    flush()
    return "".join(paragraphs) or "<w:p/>"


NAMESPACES = (f'{W} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"')


def package(content: str, margins: tuple[int, int, int, int] = (1134, 1134, 1134, 1134), photo: bytes | None = None) -> bytes:
    """A .docx of body XML; margins (top, right, bottom, left) in twips."""
    top, right, bottom, left = margins
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NAMESPACES}><w:body>{content}'
                f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="{top}" w:right="{right}" '
                f'w:bottom="{bottom}" w:left="{left}" w:header="400" w:footer="400" w:gutter="0"/>'
                "</w:sectPr></w:body></w:document>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS.format(image=IMAGE_REL if photo else ""))
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", STYLES)
        if photo:
            archive.writestr("word/media/photo.jpg", photo)
    return buffer.getvalue()


def to_docx(markdown: str) -> bytes:
    return package(body(markdown))


# ----------------------------------------------------------------------
# The CV in the user's design. Measured from the original PDF (A4, Word
# 2016, Calibri): margins 15 mm, name 20 pt, headline 11.5 pt, headings
# 10.5 pt with a rule, body 9.5 pt; colours below.
# ----------------------------------------------------------------------

DARK = "1F4E5F"    # name, section headings and their rule
ACCENT = "3D7A8C"  # headline, company / school names
TEXT_COLOR = "2A2A2A"
GREY = "6B6B6B"    # contact line, dates
PHOTO_EMU = 70 * 12700  # 70 pt square
CV_MARGINS = (500, 850, 560, 850)
CONTENT_TWIPS = 11906 - 850 - 850
PHOTO_TWIPS = 1500


def run(text: str, *, bold=False, italic=False, color: str | None = None, size: float | None = 9.5, caps=False) -> str:
    if not text:
        return ""
    props = "".join((
        "<w:b/>" if bold else "", "<w:i/>" if italic else "", "<w:caps/>" if caps else "",
        f'<w:color w:val="{color}"/>' if color else "",
        f'<w:sz w:val="{round(size * 2)}"/><w:szCs w:val="{round(size * 2)}"/>' if size else ""))
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def para(runs_xml: str, *, after=0, before=0, keep=False, rule=False, bullet=False, align: str | None = None) -> str:
    props = [f'<w:spacing w:before="{before}" w:after="{after}" w:line="240" w:lineRule="auto"/>']
    if keep:
        props.insert(0, "<w:keepNext/>")
    if rule:
        props.insert(0, f'<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="1" w:color="{DARK}"/></w:pBdr>')
    if bullet:
        props += ['<w:tabs><w:tab w:val="left" w:pos="350"/></w:tabs>', '<w:ind w:left="350" w:hanging="210"/>']
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    return f"<w:p><w:pPr>{''.join(props)}</w:pPr>{runs_xml}</w:p>"


def photo_run() -> str:
    return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{PHOTO_EMU}" cy="{PHOTO_EMU}"/>'
            '<wp:docPr id="1" name="Foto"/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:nvPicPr><pic:cNvPr id="1" name="photo.jpg"/><pic:cNvPicPr/></pic:nvPicPr>'
            '<pic:blipFill><a:blip r:embed="rIdPhoto"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{PHOTO_EMU}" cy="{PHOTO_EMU}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            "</wp:inline></w:drawing></w:r>")


def header_table(left: str, photo: bool) -> str:
    """Name, headline and contact on the left, the photo on the right (no borders)."""
    if not photo:
        return left
    text_width = CONTENT_TWIPS - PHOTO_TWIPS
    none = "".join(f'<w:{side} w:val="nil"/>' for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_TWIPS}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
            f'<w:tblBorders>{none}</w:tblBorders><w:tblCellMar><w:left w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/>'
            f'</w:tblCellMar></w:tblPr><w:tblGrid><w:gridCol w:w="{text_width}"/><w:gridCol w:w="{PHOTO_TWIPS}"/></w:tblGrid>'
            f'<w:tr><w:tc><w:tcPr><w:tcW w:w="{text_width}" w:type="dxa"/></w:tcPr>{left}</w:tc>'
            f'<w:tc><w:tcPr><w:tcW w:w="{PHOTO_TWIPS}" w:type="dxa"/></w:tcPr>{para(photo_run(), align="right")}</w:tc></w:tr></w:tbl>')


def bullet_para(text: str) -> str:
    return para(run("•\t", color=TEXT_COLOR) + run(text), bullet=True, after=10)


def cv_docx(cv: dict, photo: bytes | None = None) -> bytes:
    from cv_layout import has, headings

    names = headings(cv)
    top = para(run(cv["name"], bold=True, color=DARK, size=20))
    if cv.get("headline"):
        top += para(run(cv["headline"], bold=True, color=ACCENT, size=11.5), after=20)
    for line in cv.get("contact", []):
        top += para(run("  •  ".join(line), color=GREY))
    parts = [header_table(top, bool(photo))]

    for section in cv["order"]:
        if not has(cv, section):
            continue
        parts.append(para(run(names[section], bold=True, color=DARK, size=10.5), before=200, after=80, keep=True, rule=True))
        if section in ("summary", "references"):
            parts.append(para(run(cv[section], italic=section == "references")))
        elif section == "projects":
            for p in cv["projects"]:
                parts.append(para(run(p["name"], bold=True, color=TEXT_COLOR) + run(f"  —  {p['description']}" if p["description"] else ""),
                                  before=40, keep=bool(p["bullets"])))
                parts += [bullet_para(b) for b in p["bullets"]]
        elif section == "experience":
            for e in cv["experience"]:
                head = run(e["title"], bold=True, color=TEXT_COLOR)
                if e["company"]:
                    head += run("  —  ", color=GREY) + run(e["company"], bold=True, color=ACCENT)
                parts.append(para(head, before=60, keep=True))
                when = "  |  ".join(x for x in (e["dates"], e["location"]) if x)
                if when:
                    parts.append(para(run(when, italic=True, color=GREY, size=9), keep=bool(e["bullets"])))
                parts += [bullet_para(b) for b in e["bullets"]]
        elif section == "skills":
            for s in cv["skills"]:
                parts.append(para(run(f"{s['label']}: {s['text']}" if s["label"] else s["text"]), after=30))
            if cv.get("languages"):
                parts.append(para(run(f"{names['languages']}:", bold=True, color=TEXT_COLOR) + run(f" {cv['languages']}")))
        elif section == "education":
            for e in cv["education"]:
                line = run(e["title"], bold=True, color=TEXT_COLOR)
                if e["org"]:
                    line += run("  —  ", color=GREY) + run(e["org"], bold=True, color=ACCENT)
                if e["dates"]:
                    line += run(f"  ({e['dates']})", italic=True, color=GREY)
                parts.append(para(line, after=20))
    return package("".join(parts), CV_MARGINS, photo)
