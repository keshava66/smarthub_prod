from __future__ import annotations

import io
import logging
from datetime import datetime as _dt_type

logger = logging.getLogger(__name__)

# Excel epoch (days since 1899-12-30 = serial number 1)
_EXCEL_EPOCH = _dt_type(1899, 12, 30)
_DATE_FMT    = "DD-MMM-YYYY"
_INT_FMT     = "0"
_FLOAT_FMT   = "0.##"

# Columns that must never be coerced to a number even if they look numeric
_TEXT_ONLY_HEADERS = {"FAILURE_MODE"}


def _is_date_col(header: str) -> bool:
    h = header.upper()
    return "DATE" in h and "DIFFERENCE" not in h


def _is_text_only(header: str) -> bool:
    return header.upper().strip() in _TEXT_ONLY_HEADERS


def _to_serial(value) -> int | None:
    """Convert a date-like value to an Excel serial number, or None."""
    try:
        import pandas as pd
        if isinstance(value, _dt_type):
            ts = value
        else:
            ts = pd.Timestamp(value)
            if pd.isna(ts):
                return None
            ts = ts.to_pydatetime().replace(tzinfo=None)
        return (ts - _EXCEL_EPOCH).days
    except Exception:
        return None


def _try_numeric(value: str):
    """Return (int|float, fmt) or None if value is not parseable as a number."""
    v = value.strip()
    if not v or v.lower() in ("none", "nat", "null"):
        return None
    try:
        return int(v), _INT_FMT
    except ValueError:
        pass
    try:
        return float(v), _FLOAT_FMT
    except ValueError:
        return None


def apply_excel_type_formatting(buf: io.BytesIO) -> bytes:
    """
    Post-process an xlsxwriter BytesIO buffer:
      - Date columns  → Excel serial numbers + DD-MMM-YYYY format
      - Numeric cols  → int / float + "0" / "0.##" format
      - Rich text     → preserved (rich_text=True)

    Returns the processed bytes, or the original bytes if anything fails.
    """
    try:
        import openpyxl as _oxl

        buf.seek(0)
        wb = _oxl.load_workbook(buf, rich_text=True)

        for ws in wb.worksheets:
            # ── Read header row ────────────────────────────────────────────
            hdr_row = list(next(ws.iter_rows(min_row=1, max_row=1), []))
            if not hdr_row:
                continue

            headers = [str(c.value or "").strip() for c in hdr_row]

            # Build sets of 1-based column indices
            date_cols    = {i + 1 for i, h in enumerate(headers) if _is_date_col(h)}
            text_only    = {i + 1 for i, h in enumerate(headers) if _is_text_only(h)}
            numeric_cols = set()  # populated on first pass below

            # ── Date pass ─────────────────────────────────────────────────
            date_converted = 0
            for col_idx in date_cols:
                for (cell,) in ws.iter_rows(
                    min_row=2, min_col=col_idx, max_col=col_idx
                ):
                    if cell.value is None or cell.value == "":
                        continue
                    # Already a real datetime — just apply format
                    if isinstance(cell.value, _dt_type):
                        cell.number_format = _DATE_FMT
                        date_converted += 1
                        continue
                    # String → serial number
                    if isinstance(cell.value, str):
                        sv = cell.value.strip()
                        if not sv or sv.lower() in ("none", "nat", "null"):
                            continue
                        # Try explicit formats first (fast + unambiguous)
                        parsed = None
                        for fmt in ("%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                                    "%m/%d/%Y", "%d/%m/%Y"):
                            try:
                                parsed = _dt_type.strptime(sv, fmt)
                                break
                            except ValueError:
                                pass
                        # Pandas fallback for any other format
                        if parsed is None:
                            try:
                                import pandas as pd
                                ts = pd.Timestamp(sv)
                                if not pd.isna(ts):
                                    parsed = ts.to_pydatetime().replace(tzinfo=None)
                            except Exception:
                                pass
                        if parsed is not None:
                            serial = (parsed - _EXCEL_EPOCH).days
                            cell.value         = serial
                            cell.data_type     = "n"
                            cell.number_format = _DATE_FMT
                            date_converted += 1

            logger.info(
                f"  [excel_utils] '{ws.title}': {date_converted} date cell(s) formatted"
            )

            # ── Numeric pass ──────────────────────────────────────────────
            # Scan all non-date, non-text-only columns; if every non-empty
            # cell in the column is numeric-parseable, convert the whole col.
            num_converted = 0
            for col_idx in range(1, len(headers) + 1):
                if col_idx in date_cols or col_idx in text_only:
                    continue

                # Collect all non-empty string cells in this column
                candidate_cells = []
                all_numeric = True
                for (cell,) in ws.iter_rows(
                    min_row=2, min_col=col_idx, max_col=col_idx
                ):
                    if not isinstance(cell.value, str):
                        # Non-string (None, already a number, rich-text): skip
                        if cell.value is not None and not isinstance(cell.value, (int, float)):
                            all_numeric = False
                            break
                        continue
                    sv = cell.value.strip()
                    if not sv or sv.lower() in ("none", "nat", "null"):
                        continue
                    result = _try_numeric(sv)
                    if result is None:
                        all_numeric = False
                        break
                    candidate_cells.append((cell, result))

                if all_numeric and candidate_cells:
                    for cell, (num_val, fmt) in candidate_cells:
                        cell.value         = num_val
                        cell.data_type     = "n"
                        cell.number_format = fmt
                        num_converted += 1

            logger.info(
                f"  [excel_utils] '{ws.title}': {num_converted} numeric cell(s) converted"
            )

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        logger.info("  [excel_utils] Post-processing complete.")
        return out.read()

    except Exception as exc:
        import traceback
        logger.warning(f"  [excel_utils] Post-processing failed (returning raw bytes): {exc}")
        logger.warning(traceback.format_exc())
        buf.seek(0)
        return buf.read()

def apply_gradient_fills(xlsx_bytes: bytes, sheet_gradients: dict) -> bytes:

    if not sheet_gradients or not any(sheet_gradients.values()):
        return xlsx_bytes

    try:
        import openpyxl as _oxl
        from openpyxl.styles import GradientFill
        from openpyxl.styles.fills import Stop

        # rich_text=True so FAILURE_MODE's per-rule colouring survives the round trip
        wb = _oxl.load_workbook(io.BytesIO(xlsx_bytes), rich_text=True)

        painted = 0
        for sheet_name, cells in sheet_gradients.items():
            if not cells or sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            for (row, col), colors in cells.items():
                n = len(colors)
                if n < 2:
                    continue
                eps = 0.001
                stops = []
                for i, hex_color in enumerate(colors):
                    argb = "FF" + hex_color.lstrip("#").upper()
                    start = round(i / n + (eps if i > 0 else 0), 4)
                    end = round((i + 1) / n - (eps if i < n - 1 else 0), 4)
                    stops.append(Stop(color=argb, position=start))
                    stops.append(Stop(color=argb, position=end))
                ws.cell(row=row, column=col).fill = GradientFill(
                    type="linear", degree=0, stop=stops
                )
                painted += 1

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        logger.info(f"  [excel_utils] {painted} gradient cell(s) applied")
        return out.read()

    except Exception as exc:
        logger.warning(f"  [excel_utils] Gradient fills failed (keeping solid colours): {exc}")
        return xlsx_bytes