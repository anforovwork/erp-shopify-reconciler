"""JSON snapshot of one run: the fetched data, the options used and the result.

The MCP server answers from snapshots, and a planned `reconcile report` will render a run again
without calling any API. It holds only
the pydantic models below, never Settings, so no credential can end up in it. Decimals are
written as JSON strings and read back as Decimal (floats are rejected by the models).
"""

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from reconciler.models import ErpData, ReconciliationResult, ShopifyData
from reconciler.reconcile import ReconcileOptions

SNAPSHOT_FORMAT_VERSION = 1


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1] = SNAPSHOT_FORMAT_VERSION
    options: ReconcileOptions
    shopify: ShopifyData
    erpnext: ErpData
    result: ReconciliationResult


def snapshot_path(directory: Path, generated_at: datetime) -> Path:
    stamp = generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"snapshot-{stamp}.json"


def save_snapshot(snapshot: Snapshot, path: Path) -> None:
    """Write the snapshot; an existing file is never overwritten (FileExistsError)."""
    write_text_atomic(path, snapshot.model_dump_json(indent=2) + "\n", overwrite=False)


def load_snapshot(path: Path) -> Snapshot:
    return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))


def write_text_atomic(path: Path, text: str, *, overwrite: bool) -> None:
    """Write to a temporary file in the same directory, then move it into place, so readers
    never see a half-written file. The file is created readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if overwrite:
            tmp.replace(path)
        else:
            os.link(tmp, path)  # fails with FileExistsError instead of replacing
    finally:
        tmp.unlink(missing_ok=True)
