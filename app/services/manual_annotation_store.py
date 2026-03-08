"""
SQLite-backed persistence for manually corrected duct annotations.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from app.core.config import settings
from app.models.schemas import DuctBBox, ManualAnnotationPayload, ManualAnnotationRecord

_STORE_LOCK = Lock()
_STORE_INITIALIZED = False


def _resolve_db_path() -> Path:
    db_path = Path(settings.manual_annotations_db_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_resolve_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_manual_annotation_store() -> None:
    global _STORE_INITIALIZED

    with _STORE_LOCK:
        if _STORE_INITIALIZED:
            return

        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    document_name TEXT,
                    page INTEGER NOT NULL,
                    x0 REAL NOT NULL,
                    y0 REAL NOT NULL,
                    x1 REAL NOT NULL,
                    y1 REAL NOT NULL,
                    label TEXT NOT NULL,
                    pressure_class TEXT,
                    dimension TEXT,
                    material TEXT,
                    confidence REAL NOT NULL,
                    orientation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manual_annotations_document_id ON manual_annotations(document_id)"
            )
            conn.commit()
        finally:
            conn.close()

        _STORE_INITIALIZED = True


def _row_to_record(row: sqlite3.Row) -> ManualAnnotationRecord:
    return ManualAnnotationRecord(
        id=int(row["id"]),
        document_id=str(row["document_id"]),
        document_name=row["document_name"],
        bbox=DuctBBox(
            x0=float(row["x0"]),
            y0=float(row["y0"]),
            x1=float(row["x1"]),
            y1=float(row["y1"]),
            page=int(row["page"]),
        ),
        label=str(row["label"]),
        pressure_class=row["pressure_class"],
        dimension=row["dimension"],
        material=row["material"],
        confidence=float(row["confidence"]),
        orientation=str(row["orientation"]),
        source="manual",
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _normalized_bbox(annotation: ManualAnnotationPayload) -> tuple[float, float, float, float]:
    x0 = min(float(annotation.bbox.x0), float(annotation.bbox.x1))
    x1 = max(float(annotation.bbox.x0), float(annotation.bbox.x1))
    y0 = min(float(annotation.bbox.y0), float(annotation.bbox.y1))
    y1 = max(float(annotation.bbox.y0), float(annotation.bbox.y1))
    return x0, y0, x1, y1


def save_manual_annotation(
    *,
    document_id: str,
    document_name: str | None,
    annotation: ManualAnnotationPayload,
) -> ManualAnnotationRecord:
    initialize_manual_annotation_store()

    normalized_document_id = str(document_id).strip()
    if not normalized_document_id:
        raise ValueError("document_id must not be empty")

    normalized_document_name = (str(document_name).strip() if document_name else None) or None

    x0, y0, x1, y1 = _normalized_bbox(annotation)

    now_iso = datetime.now(UTC).isoformat()

    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO manual_annotations (
                document_id,
                document_name,
                page,
                x0,
                y0,
                x1,
                y1,
                label,
                pressure_class,
                dimension,
                material,
                confidence,
                orientation,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_document_id,
                normalized_document_name,
                int(annotation.bbox.page),
                x0,
                y0,
                x1,
                y1,
                str(annotation.label).strip(),
                annotation.pressure_class,
                annotation.dimension,
                annotation.material,
                float(annotation.confidence),
                str(annotation.orientation),
                now_iso,
                now_iso,
            ),
        )
        new_id = int(cursor.lastrowid)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM manual_annotations WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise RuntimeError("Failed to read newly saved manual annotation")

    return _row_to_record(row)


def list_manual_annotations(document_id: str) -> list[ManualAnnotationRecord]:
    initialize_manual_annotation_store()

    normalized_document_id = str(document_id).strip()
    if not normalized_document_id:
        return []

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM manual_annotations
            WHERE document_id = ?
            ORDER BY id ASC
            """,
            (normalized_document_id,),
        ).fetchall()
    finally:
        conn.close()

    return [_row_to_record(row) for row in rows]


def update_manual_annotation(
    annotation_id: int,
    annotation: ManualAnnotationPayload,
) -> ManualAnnotationRecord | None:
    initialize_manual_annotation_store()

    x0, y0, x1, y1 = _normalized_bbox(annotation)
    now_iso = datetime.now(UTC).isoformat()

    conn = _connect()
    try:
        cursor = conn.execute(
            """
            UPDATE manual_annotations
            SET
                page = ?,
                x0 = ?,
                y0 = ?,
                x1 = ?,
                y1 = ?,
                label = ?,
                pressure_class = ?,
                dimension = ?,
                material = ?,
                confidence = ?,
                orientation = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                int(annotation.bbox.page),
                x0,
                y0,
                x1,
                y1,
                str(annotation.label).strip(),
                annotation.pressure_class,
                annotation.dimension,
                annotation.material,
                float(annotation.confidence),
                str(annotation.orientation),
                now_iso,
                int(annotation_id),
            ),
        )
        if cursor.rowcount <= 0:
            conn.commit()
            return None

        conn.commit()
        row = conn.execute(
            "SELECT * FROM manual_annotations WHERE id = ?",
            (int(annotation_id),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return _row_to_record(row)


def delete_manual_annotation(annotation_id: int) -> bool:
    initialize_manual_annotation_store()

    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM manual_annotations WHERE id = ?",
            (int(annotation_id),),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
