from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from ingestion import ZoningDocument

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegalChunk:
    doc_id: str
    doc_title: str
    document_type: str
    established_date: Optional[str]

    article_id: Optional[str]        
    heading: str                    
    text: str                       


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


_HEADING_ARTICLE_RE = re.compile(
    r"(?mi)^(?P<prefix>#{1,6}\s*)(?P<label>artikel)\s+(?P<num>(?:\d+(?:\.\d+)*|[IVXLCDM]+))\b(?P<rest>[^\n]*)$"
)

_BOLD_ARTICLE_RE = re.compile(
    r"(?mi)^(?P<prefix>\*\*)(?P<label>artikel)\s+(?P<num>(?:\d+(?:\.\d+)*|[IVXLCDM]+))\b(?P<rest>[^*\n]*)\*\*(?P<tail>.*)$"
)

_PLAIN_ARTICLE_RE = re.compile(
    r"(?mi)^(?P<label>artikel)\s+(?P<num>(?:\d+(?:\.\d+)*|[IVXLCDM]+))\b(?P<rest>[^\n]*)$"
)


def _find_article_headers(text: str) -> List[re.Match]:
    matches = list(_HEADING_ARTICLE_RE.finditer(text))
    if matches:
        return matches

    matches = list(_BOLD_ARTICLE_RE.finditer(text))
    if matches:
        return matches

    return list(_PLAIN_ARTICLE_RE.finditer(text))


class MarkdownParser:
    
    def split_by_article(self, document: ZoningDocument) -> List[LegalChunk]:
        text = _normalize_text(document.text)
        matches = _find_article_headers(text)

        if not matches:
            LOGGER.warning("No article boundaries detected for doc='%s'. Returning single chunk.", document.title)
            return [
                LegalChunk(
                    doc_id=document.id,
                    doc_title=document.title,
                    document_type=document.document_type,
                    established_date=document.established_date,
                    article_id=None,
                    heading="(Unsegmented document)",
                    text=text,
                )
            ]

        chunks: List[LegalChunk] = []

        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunk_text = text[start:end].strip()

            article_id = m.group("num").strip() if "num" in m.groupdict() else None
            heading_line = m.group(0).strip()

            chunks.append(
                LegalChunk(
                    doc_id=document.id,
                    doc_title=document.title,
                    document_type=document.document_type,
                    established_date=document.established_date,
                    article_id=article_id,
                    heading=heading_line,
                    text=chunk_text,
                )
            )

        return chunks


def estimate_tokens(text: str, model: str = "gpt-4o") -> int:
    try:
        import tiktoken

        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)
