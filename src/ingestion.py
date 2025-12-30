from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LOGGER = logging.getLogger(__name__)

class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_address: str
    postcode: str
    municipality: str
    province: str
    country: str

class Maatvoering(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: float

class ZoningMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bestemmingsvlakken: List[str]
    maatvoeringen: Optional[List[Maatvoering]] = None

class ZoningDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    text: str

    temporaryParts: List[Dict[str, Any]] = Field(default_factory=list)

    document_type: str
    document_type_description: Optional[str] = None
    established_date: Optional[str] = None

    def established_datetime(self) -> Optional[datetime]:
        if not self.established_date:
            return None

        raw = self.established_date.strip()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass

        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None

class ZoningPlanFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: Address
    zoning_documents: List[ZoningDocument]
    zoning_metadata: ZoningMetadata


@dataclass(frozen=True)
class DocumentFilterConfig:
    allowed_document_types: Tuple[str, ...] = ("Bestemmingsplan", "Omgevingsplan")
    exclude_title_contains: Tuple[str, ...] = ("parapluplan",)

    sort_by_established_date_desc: bool = True


class ZoningDataLoader:
    def __init__(self, data_dir: str | Path, filter_config: Optional[DocumentFilterConfig] = None) -> None:
        self.data_dir = Path(data_dir)
        self.filter_config = filter_config or DocumentFilterConfig()

    def load_file(self, filename: str) -> ZoningPlanFile:
        """
        Load and validate a zoning JSON file into a typed model.
        """
        path = self.data_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Zoning file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            raw: Dict[str, Any] = json.load(f)

        try:
            return ZoningPlanFile.model_validate(raw)
        except ValidationError as e:
            raise ValueError(f"Invalid zoning JSON schema in {filename}: {e}") from e

    def iter_json_files(self) -> Iterable[str]:
        """
        Iterate over all *.json files in the data directory.
        """
        if not self.data_dir.exists():
            raise FileNotFoundError(f"Data directory does not exist: {self.data_dir}")
        for p in sorted(self.data_dir.glob("*.json")):
            yield p.name

    def filter_documents(self, documents: Sequence[ZoningDocument]) -> List[ZoningDocument]:
        cfg = self.filter_config
        allowed = {t.lower() for t in cfg.allowed_document_types}

        def is_allowed(doc: ZoningDocument) -> bool:
            title = (doc.title or "").lower()
            doc_type = (doc.document_type or "").lower()

            if any(bad in title for bad in cfg.exclude_title_contains):
                return False
            if doc_type not in allowed:
                return False
            return True

        filtered = [d for d in documents if is_allowed(d)]

        if cfg.sort_by_established_date_desc:
            filtered.sort(
                key=lambda d: d.established_datetime() or datetime.min,
                reverse=True,
            )

        return filtered
    


