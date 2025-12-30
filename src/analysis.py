from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field
from enum import Enum

from ingestion import ZoningMetadata, ZoningPlanFile, ZoningDocument
from parsing import LegalChunk, MarkdownParser, estimate_tokens

LOGGER = logging.getLogger(__name__)


class PermitStatus(str, Enum):
    YES = "Yes"
    NO = "No"
    CONDITIONAL = "Conditional"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_document: str
    article: Optional[str] = None
    excerpt: str = Field(..., description="Short excerpt (<= ~30 words) from the provided text.")
    relevance: str = Field(..., description="Why this excerpt matters for the decision.")


class ZoningAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permit_free: PermitStatus = Field(..., description="Yes / No / Conditional")
    summary: str = Field(..., description="Concise explanation referencing the evidence.")
    cited_evidence: List[Evidence] = Field(..., min_length=1)

    suggested_changes: Optional[str] = Field(
        default=None, description="Minor changes that could make the plan permit-free / compliant."
    )
    assumptions: List[str] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ResidentPlan:
    structure: str = "bijbehorend bouwwerk (outbuilding)"
    area_m2: float = 20.0
    height_m: float = 3.0
    intended_use: str = "Living space (verblijfsgebied), subordinate to the main house"

    def as_text(self) -> str:
        return (
            f"Structure: {self.structure}\n"
            f"Area: {self.area_m2} m²\n"
            f"Height: {self.height_m} m\n"
            f"Use: {self.intended_use}\n"
        )


@dataclass(frozen=True)
class ContextBuilderConfig:
    model_for_token_estimation: str = "gpt-4o"
    max_context_tokens: int = 10_000
    max_chunks: int = 40

    include_definitions: bool = True

    force_living_space_chunks: bool = True


class ContextBuilder:
    PERMIT_FREE_SIGNALS: Tuple[str, ...] = (
        "vergunningsvrij",
        "vergunningvrij",
        "zonder omgevingsvergunning",
        "geen omgevingsvergunning",
        "niet vergunningplichtig",
        "uitzondering op de vergunningplicht",
        "is niet van toepassing",
    )

    CONSTRUCTION_GATE: Tuple[str, ...] = (
        "bouw", "bouwen", "bouwwerk", "bouwwerken",
        "bijbehorend", "bijgebouw", "erf", "achtererf",
        "aanbouw", "uitbouw", "erker", "berging", "schuur", "tuinhuis", "garage", "carport",
        "dakterras", "balkon",
        "omgevingsplanactiviteit", "bouwactiviteit",
        "vergunningplicht", "omgevingsvergunning",
    )

    PLAN_TERMS: Tuple[str, ...] = (
        "bijbehorend bouwwerk",
        "bijgebouw",
        "erfbebouwing",
        "achtererfgebied",
        "oppervlakte",
        "bouwhoogte",
        "goothoogte",
        "m2",
    )

    LIVING_SPACE_TERMS: Tuple[str, ...] = (
        "verblijfsgebied",
        "verblijfsruimte",
        "woonfunctie",
        "bewoning",
        "wonen",
        "permanente bewoning",
    )

    def __init__(self, parser: Optional[MarkdownParser] = None, cfg: Optional[ContextBuilderConfig] = None) -> None:
        self.parser = parser or MarkdownParser()
        self.cfg = cfg or ContextBuilderConfig()

    @staticmethod
    def _normalize_designation_terms(bestemmingsvlakken: Sequence[str]) -> List[str]:
        terms: List[str] = []
        for raw in bestemmingsvlakken:
            s = raw.strip().lower()
            if not s:
                continue
            terms.append(s)

            if "-" in s:
                parts = [p.strip() for p in s.split("-") if p.strip()]
                if parts:
                    terms.append(parts[-1]) 

            s2 = re.sub(r"\s+\d+$", "", s).strip()
            if s2 and s2 != s:
                terms.append(s2)

        seen = set()
        out: List[str] = []
        for t in terms:
            if t not in seen:
                out.append(t)
                seen.add(t)
        return out

    def _chunk_score(self, chunk: LegalChunk, zoning_terms: Sequence[str], plan: ResidentPlan) -> int:
        text = f"{chunk.heading}\n{chunk.text}".lower()
        score = 0

        for sig in self.PERMIT_FREE_SIGNALS:
            if sig in text:
                score += 50

        for t in self.PLAN_TERMS:
            if t in text:
                score += 20

        for t in zoning_terms:
            if t and t in text:
                score += 10

        if any(term in plan.intended_use.lower() for term in ("verblijfsgebied", "living space", "woonfunctie")):
            for t in self.LIVING_SPACE_TERMS:
                if t in text:
                    score += 25

        if "uitzondering" in text:
            score += 8
        if "vergunningplicht" in text:
            score += 8

        return score

    def _passes_gate(self, chunk: LegalChunk, zoning_terms: Sequence[str], plan: ResidentPlan) -> bool:
        text = f"{chunk.heading}\n{chunk.text}".lower()

        if any(sig in text for sig in self.PERMIT_FREE_SIGNALS):
            return True

        if any(z in text for z in zoning_terms):
            return True

        if any(k in text for k in self.CONSTRUCTION_GATE):
            return True

        if any(term in plan.intended_use.lower() for term in ("verblijfsgebied", "living space", "woonfunctie")):
            if any(t in text for t in self.LIVING_SPACE_TERMS):
                return True

        return False

    def build_context(self, zoning_plan: ZoningPlanFile, documents: Sequence[ZoningDocument], plan: ResidentPlan) -> Tuple[str, List[LegalChunk]]:
        cfg = self.cfg
        zoning_terms = self._normalize_designation_terms(zoning_plan.zoning_metadata.bestemmingsvlakken)

        all_chunks: List[LegalChunk] = []
        for doc in documents:
            all_chunks.extend(self.parser.split_by_article(doc))

        forced: List[LegalChunk] = []
        if cfg.include_definitions:
            for c in all_chunks:
                h = c.heading.lower()
                if "begrip" in h or "begripsbepal" in h:
                    forced.append(c)
                    break

        if cfg.force_living_space_chunks and any(term in plan.intended_use.lower() for term in ("verblijfsgebied", "living space", "woonfunctie")):
            for c in all_chunks:
                text = f"{c.heading}\n{c.text}".lower()
                if "verblijfsgebied" in text or "woonfunctie" in text:
                    forced.append(c)

        scored: List[Tuple[int, LegalChunk]] = []
        for c in all_chunks:
            if not self._passes_gate(c, zoning_terms, plan):
                continue
            score = self._chunk_score(c, zoning_terms, plan)
            if score > 0:
                scored.append((score, c))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected: List[LegalChunk] = []
        seen_ids = set()

        def add_chunk(c: LegalChunk) -> None:
            cid = (c.doc_id, c.article_id, c.heading)
            if cid in seen_ids:
                return
            selected.append(c)
            seen_ids.add(cid)

        for c in forced:
            add_chunk(c)

        for _, c in scored:
            if len(selected) >= cfg.max_chunks:
                break
            add_chunk(c)

        context_parts: List[str] = []
        tokens_used = 0

        for c in selected:
            block = (
                f"[SOURCE] {c.doc_title} | doc_id={c.doc_id} | type={c.document_type} | date={c.established_date}\n"
                f"[ARTICLE] {c.article_id or 'N/A'}\n"
                f"[HEADING] {c.heading}\n"
                f"{c.text}\n"
            )
            block_tokens = estimate_tokens(block, model=cfg.model_for_token_estimation)
            if tokens_used + block_tokens > cfg.max_context_tokens:
                break
            context_parts.append(block)
            tokens_used += block_tokens

        context = "\n\n".join(context_parts).strip()

        LOGGER.info(
            "ContextBuilder selected %d chunks (assembled ~%d tokens) for address=%s",
            len(context_parts),
            tokens_used,
            zoning_plan.address.display_address,
        )

        return context, selected


class ZoningAnalyzer:
    def __init__(self, api_key: str, model: str = "gpt-4o", temperature: float = 0.0) -> None:
        self.api_key = api_key
        self.model = model
        self.temperature = temperature

    def analyze(self, *, plan: ResidentPlan, zoning_context: str, metadata: ZoningMetadata, address: str) -> ZoningAssessment:
        try:
            from langchain_openai import ChatOpenAI
            from langchain_core.prompts import ChatPromptTemplate
        except Exception as e:
            raise ImportError(
                "Missing langchain dependencies. Run: pip install -r requirements.txt"
            ) from e

        llm = ChatOpenAI(
            model=self.model,
            temperature=self.temperature,
            api_key=self.api_key,
        )

        system = (
            "You are a Dutch zoning & permitting expert (Ruimtelijke Ordening / Omgevingswet).\n"
            "Your task: Decide if the resident's plan is PERMIT-FREE (vergunningsvrij) at the given address.\n\n"
            "HARD RULES (from assignment):\n"
            "1) Use ONLY the provided 'Relevant Excerpts'.\n"
            "2) Answer 'Yes' ONLY if the excerpts explicitly indicate permit-free, e.g. 'vergunningsvrij', "
            "'zonder omgevingsvergunning', 'niet vergunningplichtig', or 'is niet van toepassing'.\n"
            "3) If a rule allows building/usage but does NOT explicitly say permit-free, answer 'No' (permit required).\n\n"
            "TRAP / HIGH-RISK NUANCE:\n"
            "- The plan is an outbuilding (bijbehorend bouwwerk) used as Living Space (verblijfsgebied / woonfunctie).\n"
            "- Outbuildings are often only permit-free for storage/hobby; living space frequently triggers permits.\n"
            "- Therefore: you MUST explicitly check whether the permit-free clause (if any) allows a verblijfsgebied/woonfunctie "
            "inside the outbuilding. If unclear, answer 'No' or 'Conditional'.\n\n"
            "OUTPUT REQUIREMENTS:\n"
            "- Provide a decision (Yes/No/Conditional).\n"
            "- Provide a concise summary.\n"
            "- Provide cited_evidence with short excerpts (<= ~30 words each).\n"
            "- If Conditional: list missing_information.\n"
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system),
                ("user",
                 "Address:\n{address}\n\n"
                 "Plot metadata (bestemmingsvlakken & maatvoeringen):\n{metadata}\n\n"
                 "Resident plan:\n{plan}\n\n"
                 "Relevant Excerpts (only source of truth):\n{context}\n"),
            ]
        )

        structured_llm = llm.with_structured_output(ZoningAssessment)
        chain = prompt | structured_llm

        result: ZoningAssessment = chain.invoke(
            {
                "address": address,
                "metadata": metadata.model_dump(),
                "plan": plan.as_text(),
                "context": zoning_context,
            }
        )

        return self._post_validate(result=result, plan=plan, zoning_context=zoning_context)

    @staticmethod
    def _post_validate(*, result: ZoningAssessment, plan: ResidentPlan, zoning_context: str) -> ZoningAssessment:
        context_lower = zoning_context.lower()

        permit_free_re = re.compile(
            r"(?i)\b(vergunningsvrij|vergunningvrij|zonder omgevingsvergunning|geen omgevingsvergunning|niet vergunningplichtig|uitzondering op de vergunningplicht|is niet van toepassing)\b"
        )

        if result.permit_free == PermitStatus.YES and not permit_free_re.search(context_lower):
            LOGGER.warning("Downgrading YES -> CONDITIONAL because no explicit permit-free signal found in context.")
            result.permit_free = PermitStatus.CONDITIONAL
            result.missing_information.append(
                "No explicit 'permit-free' language found in the provided excerpts; verify complete applicable articles."
            )

        if "verblijfsgebied" in plan.intended_use.lower() or "living space" in plan.intended_use.lower():
            if "Living space in outbuilding is high-risk" not in result.risk_flags:
                result.risk_flags.append("Living space in outbuilding is high-risk")

        return result
