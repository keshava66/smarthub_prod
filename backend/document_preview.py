from __future__ import annotations

"""Read-only previews for office documents pulled out of object storage.

Everything here takes the raw bytes of a file and hands back the same shape the
S3 explorer's preview endpoint already speaks:

    {"mode": "document", "blocks": [...]}      docx / doc / rtf / pptx
    {"mode": "sheets",   "sheets": [...]}      xlsx / xlsm / xls
    {"mode": "inline",   "inline_kind": "pdf"} the bytes are really a PDF
    {"mode": "unsupported", "message": "..."}  nothing useful to show

Blocks are structured rather than HTML on purpose - the browser renders them as
plain elements, so a document out of a landing bucket can never inject markup
into the portal.

The office formats (docx, xlsx via openpyxl, pptx) are all zip + XML and are
read with the standard library. Legacy .doc and .xls are the awkward ones:
.doc is parsed out of the OLE2 container when `olefile` is installed, and .xls
needs `xlrd`. Both degrade to a clear message instead of an exception.
"""

import io
import logging
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, time

logger = logging.getLogger(__name__)

# Caps - a preview pane is not a document viewer, it is the first screenful.
MAX_BLOCKS = 600
MAX_CHARS = 400_000
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 40
MAX_SHEETS = 15
MAX_SHEET_ROWS = 300
MAX_SHEET_COLS = 60
MAX_CELL_CHARS = 600

DOCUMENT_EXTENSIONS = {"docx", "docm", "doc", "rtf"}
SHEET_EXTENSIONS = {"xlsx", "xlsm", "xltx", "xls"}
SLIDE_EXTENSIONS = {"pptx", "pptm"}
SUPPORTED_EXTENSIONS = DOCUMENT_EXTENSIONS | SHEET_EXTENSIONS | SLIDE_EXTENSIONS

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _unsupported(message: str) -> dict:
    return {"mode": "unsupported", "message": message}


def sniff(data: bytes) -> str:
    """What the bytes actually are, ignoring whatever the file is called.

    Export jobs rename things constantly - a regulatory system that writes
    "report.doc" is just as likely to have written RTF, HTML or a real PDF into
    it, and guessing from the extension alone is how you get a screen of mojibake.
    """
    head = data[:8]
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:5] == b"{\\rtf":
        return "rtf"
    if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole2"
    stripped = data[:1024].lstrip()[:64].lower()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<html") or stripped.startswith(b"<!doctype html"):
        return "markup"
    return "unknown"


# ============================================================================
# Entry point
# ============================================================================

def preview(data: bytes, ext: str) -> dict:
    """Parse `data` into a preview payload. Never raises - a parser blowing up
    on one odd file should show a message, not 500 the detail pane."""
    ext = (ext or "").lower()
    kind = sniff(data)

    if kind == "pdf":
        return {"mode": "inline", "inline_kind": "pdf"}

    try:
        if kind == "rtf":
            return _from_rtf(data)

        if ext in SHEET_EXTENSIONS:
            if kind == "zip":
                return _from_xlsx(data)
            if kind == "ole2":
                return _from_xls(data)
            return _unsupported("This does not look like a spreadsheet — download it to open it locally.")

        if ext in SLIDE_EXTENSIONS:
            if kind != "zip":
                return _unsupported("This does not look like a PowerPoint file — download it to open it locally.")
            return _from_pptx(data)

        # Word: .docx is a zip, .doc is OLE2, and either name can hold either.
        if kind == "zip":
            names = _zip_names(data)
            if any(n.startswith("word/") for n in names):
                return _from_docx(data)
            if any(n.startswith("xl/") for n in names):
                return _from_xlsx(data)
            if any(n.startswith("ppt/") for n in names):
                return _from_pptx(data)
            return _unsupported("This zip does not contain an office document.")

        if kind == "ole2":
            return _from_doc(data)

        if kind == "markup":
            return _from_markup(data)

        return _unsupported("No preview for this file type — download it to open it locally.")
    except Exception as exc:  # noqa: BLE001 - a preview is never worth an error page
        logger.warning("document_preview: %s failed: %s", ext or kind, exc, exc_info=True)
        return _unsupported(f"Could not read this document: {exc}")


# ============================================================================
# Shared block helpers
# ============================================================================

def _document(blocks: list, truncated: bool = False) -> dict:
    if not blocks:
        return _unsupported("This document has no readable text — download it to open it locally.")
    return {"mode": "document", "blocks": blocks, "truncated": truncated}


def _clip(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")
    return text[:MAX_CELL_CHARS]


def _blocks_from_lines(lines) -> list:
    """Paragraph blocks from plain lines, with runs of blanks collapsed."""
    blocks, chars, blank = [], 0, False
    for line in lines:
        line = _clip(line).strip()
        if not line:
            blank = True
            continue
        if blank and blocks:
            blocks.append({"type": "spacer"})
        blank = False
        blocks.append({"type": "para", "text": line})
        chars += len(line)
        if len(blocks) >= MAX_BLOCKS or chars >= MAX_CHARS:
            break
    return blocks


# ============================================================================
# docx
# ============================================================================

def _zip_names(data: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def _docx_para_text(node) -> str:
    parts = []
    for el in node.iter():
        tag = el.tag
        if tag == f"{_W}t":
            parts.append(el.text or "")
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_heading_level(node) -> int:
    pr = node.find(f"{_W}pPr")
    if pr is None:
        return 0
    style = pr.find(f"{_W}pStyle")
    val = (style.get(f"{_W}val") if style is not None else "") or ""
    if val.lower() in ("title", "subtitle"):
        return 1
    match = re.match(r"heading\s*(\d)", val, re.I)
    return int(match.group(1)) if match else 0


def _docx_is_list(node) -> bool:
    pr = node.find(f"{_W}pPr")
    if pr is None:
        return False
    if pr.find(f"{_W}numPr") is not None:
        return True
    style = pr.find(f"{_W}pStyle")
    val = (style.get(f"{_W}val") if style is not None else "") or ""
    return bool(re.match(r"list(bullet|paragraph|number)", val, re.I))


def _from_docx(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError:
            return _unsupported("This .docx has no document body — download it to open it locally.")

    body = ET.fromstring(xml).find(f"{_W}body")
    if body is None:
        return _unsupported("This .docx has no document body — download it to open it locally.")

    blocks, chars = [], 0
    for child in body:
        if len(blocks) >= MAX_BLOCKS or chars >= MAX_CHARS:
            return _document(blocks, truncated=True)

        if child.tag == f"{_W}p":
            text = _clip(_docx_para_text(child)).strip()
            if not text:
                continue
            level = _docx_heading_level(child)
            if level:
                blocks.append({"type": "heading", "level": level, "text": text})
            elif _docx_is_list(child):
                blocks.append({"type": "bullet", "text": text})
            else:
                blocks.append({"type": "para", "text": text})
            chars += len(text)

        elif child.tag == f"{_W}tbl":
            rows = []
            for tr in child.findall(f"{_W}tr"):
                cells = [
                    _clip(" ".join(
                        _docx_para_text(p).strip() for p in tc.findall(f"{_W}p")
                    ).strip())
                    for tc in tr.findall(f"{_W}tc")
                ]
                rows.append(cells[:MAX_TABLE_COLS])
                if len(rows) >= MAX_TABLE_ROWS + 1:
                    break
            if not rows:
                continue
            width = max(len(r) for r in rows)
            header = [rows[0][i] if i < len(rows[0]) else "" for i in range(width)]
            body_rows = [[r[i] if i < len(r) else "" for i in range(width)] for r in rows[1:]]
            blocks.append({"type": "table", "columns": header, "rows": body_rows})
            chars += sum(len(c) for c in header) + sum(len(c) for r in body_rows for c in r)

    return _document(blocks)


# ============================================================================
# pptx
# ============================================================================

def _slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _from_pptx(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = sorted(
            (n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=_slide_sort_key,
        )
        if not slides:
            return _unsupported("This deck has no slides — download it to open it locally.")

        blocks, chars = [], 0
        for index, name in enumerate(slides, start=1):
            blocks.append({"type": "heading", "level": 2, "text": f"Slide {index}"})
            root = ET.fromstring(archive.read(name))
            for para in root.iter(f"{_A}p"):
                text = _clip("".join(t.text or "" for t in para.iter(f"{_A}t"))).strip()
                if not text:
                    continue
                blocks.append({"type": "para", "text": text})
                chars += len(text)
            if len(blocks) >= MAX_BLOCKS or chars >= MAX_CHARS:
                return _document(blocks, truncated=True)

    return _document(blocks)


# ============================================================================
# Spreadsheets
# ============================================================================

def _cell_value(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, (int, float, bool)):
        return value
    return _clip(str(value))


def _trim_trailing_blanks(rows: list) -> list:
    """Drop the empty rows and columns off the end - a read capped at 60
    columns otherwise pads every row out to 60, and the grid shows a screenful
    of empty headers for a four-column sheet."""
    while rows and all(c in (None, "") for c in rows[-1]):
        rows.pop()
    width = 0
    for row in rows:
        for i, cell in enumerate(row):
            if cell not in (None, ""):
                width = max(width, i + 1)
    return [row[:width] for row in rows] if width else []


def _from_xlsx(data: bytes) -> dict:
    try:
        import openpyxl
    except ImportError:
        return _unsupported("openpyxl is not installed on the API host, so spreadsheets cannot be previewed.")

    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets, truncated = [], len(book.sheetnames) > MAX_SHEETS
        for name in book.sheetnames[:MAX_SHEETS]:
            worksheet = book[name]
            rows = [
                [_cell_value(v) for v in row[:MAX_SHEET_COLS]]
                for row in worksheet.iter_rows(
                    max_row=MAX_SHEET_ROWS + 1, max_col=MAX_SHEET_COLS, values_only=True
                )
            ]
            rows = _trim_trailing_blanks(rows)
            sheets.append(_sheet_payload(
                name, rows,
                total_rows=worksheet.max_row or len(rows),
                total_cols=worksheet.max_column or (len(rows[0]) if rows else 0),
            ))
    finally:
        book.close()

    if not sheets:
        return _unsupported("This workbook has no sheets.")
    return {"mode": "sheets", "sheets": sheets, "truncated": truncated or any(s["truncated"] for s in sheets)}


def _from_xls(data: bytes) -> dict:
    try:
        import xlrd
    except ImportError:
        return _unsupported(
            "Legacy .xls preview needs the xlrd package on the API host. "
            "Download the file, or re-save it as .xlsx."
        )

    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    sheets, truncated = [], book.nsheets > MAX_SHEETS
    for index in range(min(book.nsheets, MAX_SHEETS)):
        worksheet = book.sheet_by_index(index)
        rows = []
        for r in range(min(worksheet.nrows, MAX_SHEET_ROWS + 1)):
            rows.append([_xls_cell(book, c) for c in worksheet.row(r)[:MAX_SHEET_COLS]])
        sheets.append(_sheet_payload(
            worksheet.name, _trim_trailing_blanks(rows),
            total_rows=worksheet.nrows, total_cols=worksheet.ncols,
        ))
    if not sheets:
        return _unsupported("This workbook has no sheets.")
    return {"mode": "sheets", "sheets": sheets, "truncated": truncated or any(s["truncated"] for s in sheets)}


def _xls_cell(book, cell):
    import xlrd

    if cell.ctype == xlrd.XL_CELL_EMPTY or cell.ctype == xlrd.XL_CELL_BLANK:
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            parts = xlrd.xldate_as_tuple(cell.value, book.datemode)
            return datetime(*parts).isoformat(sep=" ") if parts[0] else f"{parts[3]:02d}:{parts[4]:02d}:{parts[5]:02d}"
        except Exception:
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return "#ERROR"
    return _cell_value(cell.value)


def _sheet_payload(name: str, rows: list, total_rows: int, total_cols: int) -> dict:
    """First row as the header, the way every spreadsheet preview does it."""
    if not rows:
        return {"name": name, "columns": [], "rows": [], "truncated": False, "row_count": 0}

    width = max(len(r) for r in rows)
    header = rows[0] + [None] * (width - len(rows[0]))
    columns = [
        str(h) if h not in (None, "") else _column_letter(i)
        for i, h in enumerate(header)
    ]
    body = [list(r) + [None] * (width - len(r)) for r in rows[1:MAX_SHEET_ROWS + 1]]
    return {
        "name": name,
        "columns": columns,
        "rows": body,
        "row_count": max(total_rows - 1, len(body)),
        "truncated": total_rows > MAX_SHEET_ROWS + 1 or total_cols > MAX_SHEET_COLS,
    }


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ============================================================================
# RTF
# ============================================================================

# Destinations whose contents are machinery, not document text.
_RTF_SKIP = {
    "fonttbl", "colortbl", "stylesheet", "listtable", "listoverridetable", "rsidtbl",
    "info", "pict", "object", "themedata", "colorschememapping", "latentstyles",
    "datastore", "generator", "xmlnstbl", "mmathpr", "wgrffmtfilter", "filetbl",
    "revtbl", "userprops", "protusertbl", "fchars", "lchars",
}

_RTF_ESCAPES = {"par": "\n", "line": "\n", "tab": "\t", "cell": "\t", "row": "\n",
                "sect": "\n", "page": "\n", "lquote": "\u2018", "rquote": "\u2019",
                "ldblquote": "\u201c", "rdblquote": "\u201d", "emdash": "\u2014",
                "endash": "\u2013", "bullet": "\u2022", "nbsp": " ", "~": " "}

_RTF_TOKEN = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|([^\\{}]+)", re.I)


def _rtf_to_text(raw: str) -> str:
    out, stack = [], []
    skip_depth = None
    depth = 0
    ignore_next_chars = 0

    for match in _RTF_TOKEN.finditer(raw):
        word, param, hexchar, symbol, brace, text = match.groups()

        if brace == "{":
            depth += 1
            stack.append(ignore_next_chars)
            continue
        if brace == "}":
            depth -= 1
            if stack:
                ignore_next_chars = stack.pop()
            if skip_depth is not None and depth < skip_depth:
                skip_depth = None
            continue

        skipping = skip_depth is not None

        if word:
            lowered = word.lower()
            if lowered in _RTF_SKIP:
                skip_depth = depth
                continue
            if skipping:
                continue
            if lowered == "u":
                # \uN is followed by N replacement bytes we have to swallow.
                code = int(param or 0)
                out.append(chr(code if code >= 0 else code + 65536))
                ignore_next_chars = 1
                continue
            if lowered in _RTF_ESCAPES:
                out.append(_RTF_ESCAPES[lowered])
            continue

        if skipping:
            continue

        if hexchar:
            if ignore_next_chars:
                ignore_next_chars -= 1
                continue
            out.append(bytes([int(hexchar, 16)]).decode("cp1252", errors="replace"))
            continue

        if symbol:
            if symbol == "*":
                skip_depth = depth
            elif symbol in ("\\", "{", "}"):
                out.append(symbol)
            continue

        if text:
            chunk = text.replace("\r", "").replace("\n", "")
            if ignore_next_chars:
                dropped = min(ignore_next_chars, len(chunk))
                ignore_next_chars -= dropped
                chunk = chunk[dropped:]
            out.append(chunk)

    return "".join(out)


def _from_rtf(data: bytes) -> dict:
    text = _rtf_to_text(data.decode("cp1252", errors="replace"))
    return _document(_blocks_from_lines(text.splitlines()))


# ============================================================================
# Markup that got saved under an office extension
# ============================================================================

_TAG = re.compile(r"<[^>]+>")
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}


def _from_markup(data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<(br|/p|/div|/tr|/h\d)[^>]*>", "\n", text)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITY.items():
        text = text.replace(entity, char)
    lines = [re.sub(r"[ \t]+", " ", line) for line in text.splitlines()]
    return _document(_blocks_from_lines(lines))


# ============================================================================
# Legacy .doc (OLE2 / Word 97-2003)
# ============================================================================

def _from_doc(data: bytes) -> dict:
    try:
        import olefile
    except ImportError:
        text = _salvage_text(data)
        if text:
            return _document(
                [{"type": "note", "text": "Rough text extraction — install olefile on the API host for a clean .doc preview."}]
                + _blocks_from_lines(text.splitlines())
            )
        return _unsupported(
            "Legacy .doc preview needs the olefile package on the API host. "
            "Download the file, or re-save it as .docx."
        )

    if not olefile.isOleFile(io.BytesIO(data)):
        return _unsupported("No preview for this file type — download it to open it locally.")

    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            streams = {"/".join(path).lower() for path in ole.listdir()}

            # An Excel or PowerPoint file that got named .doc.
            if "workbook" in streams or "book" in streams:
                return _from_xls(data)
            if "powerpoint document" in streams:
                return _unsupported("This is a PowerPoint file — download it to open it locally.")
            if "worddocument" not in streams:
                return _unsupported("No preview for this file type — download it to open it locally.")

            word = ole.openstream("WordDocument").read()
            table_name = "1Table" if (struct.unpack_from("<H", word, 0x0A)[0] & 0x0200) else "0Table"
            table = ole.openstream(table_name).read() if table_name.lower() in streams else b""
        text = _doc_text(word, table)
    except Exception as exc:  # noqa: BLE001 - truncated or damaged container
        logger.info("document_preview: .doc container unreadable (%s), salvaging text", exc)
        text = ""

    if not text.strip():
        text = _salvage_text(data)
    if not text.strip():
        return _unsupported("This document has no readable text — download it to open it locally.")

    return _document(_blocks_from_lines(text.splitlines()))


def _doc_text(word: bytes, table: bytes) -> str:
    """Word 97 keeps its text in the WordDocument stream, but chopped into
    pieces that the piece table in the table stream stitches back together -
    and each piece is either cp1252 or UTF-16. Without walking that table you
    get the pieces in the wrong order with the deleted ones still in."""
    try:
        fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
        clx = table[fc_clx:fc_clx + lcb_clx]
        pieces = _piece_table(clx)
    except Exception as exc:  # noqa: BLE001
        logger.debug("document_preview: piece table unreadable (%s), falling back", exc)
        pieces = []

    if not pieces:
        # Word 6/95 and simple files: one flat run between fcMin and fcMac.
        fc_min, fc_mac = struct.unpack_from("<II", word, 0x18)
        chunk = word[fc_min:fc_mac]
        return _normalise_doc_text(chunk.decode("cp1252", errors="replace"))

    out = []
    for start, end, compressed in pieces:
        chunk = word[start:end]
        out.append(chunk.decode("cp1252" if compressed else "utf-16-le", errors="replace"))
    return _normalise_doc_text("".join(out))


def _piece_table(clx: bytes):
    """Pull the PlcPcd out of the CLX: a run of 0x01 property blobs, then the
    0x02 block holding the character-position array and the piece descriptors."""
    offset = 0
    while offset < len(clx):
        kind = clx[offset]
        if kind == 0x01:                                    # Prc - skip it
            size = struct.unpack_from("<H", clx, offset + 1)[0]
            offset += 3 + size
            continue
        if kind != 0x02:
            return []
        lcb = struct.unpack_from("<I", clx, offset + 1)[0]
        plc = clx[offset + 5:offset + 5 + lcb]
        break
    else:
        return []

    count = (len(plc) - 4) // 12                            # 4 bytes per CP + 8 per PCD
    if count <= 0:
        return []

    pieces = []
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    pcd_start = (count + 1) * 4
    for i in range(count):
        fc = struct.unpack_from("<I", plc, pcd_start + i * 8 + 2)[0]
        compressed = bool(fc & 0x40000000)
        base = (fc & 0x3FFFFFFF) // 2 if compressed else (fc & 0x3FFFFFFF)
        length = cps[i + 1] - cps[i]
        span = length if compressed else length * 2
        if length <= 0 or base < 0:
            continue
        pieces.append((base, base + span, compressed))
    return pieces


def _normalise_doc_text(text: str) -> str:
    text = text.replace("\r", "\n").replace("\x07", "\n").replace("\x0b", "\n")
    text = text.replace("\x0c", "\n").replace("\x13", "").replace("\x14", "").replace("\x15", "")
    text = text.replace("\u2013", "-").replace("\x1e", "-").replace("\x1f", "")
    # Field codes (HYPERLINK "...", PAGEREF, ...) sit inline and are noise here.
    text = re.sub(r"\n?\s*(HYPERLINK|PAGEREF|TOC|SEQ|REF|EMBED|INCLUDEPICTURE)\s+[^\n]*", "", text)
    text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _salvage_text(data: bytes) -> str:
    """Last resort: pull the readable runs out and hope. Better than telling
    someone their file is unpreviewable when half of it is plain prose."""
    runs = re.findall(rb"[\x20-\x7e\xa0-\xff]{6,}", data)
    text = "\n".join(r.decode("cp1252", errors="replace") for r in runs)
    # Word litters the stream with style and font names; drop the obvious ones.
    noise = re.compile(
        r"^(Times New Roman|Arial|Calibri|Cambria|Symbol|Wingdings|Normal|Default Paragraph Font|"
        r"Microsoft Word|MSWordDoc|Word\.Document|Root Entry|CompObj|ObjectPool|SummaryInformation|"
        r"DocumentSummaryInformation|WordDocument|\d?Table|[A-Za-z]:\\.*|\W*)$"
    )
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and not noise.match(line))
