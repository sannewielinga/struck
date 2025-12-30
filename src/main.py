from __future__ import annotations

import argparse
import json
import logging
import os
from typing import List

from dotenv import load_dotenv

from ingestion import ZoningDataLoader
from parsing import MarkdownParser
from analysis import ContextBuilder, ContextBuilderConfig, ResidentPlan, ZoningAnalyzer


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s - %(message)s")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Permit-free zoning analysis (Struck take-home).")
    p.add_argument("--data-dir", default="data", help="Directory containing zoning_plan_*.json files.")
    p.add_argument("--files", nargs="*", default=None, help="Optional list of specific JSON files to process.")
    p.add_argument("--model", default="gpt-4o", help="OpenAI model name (via LangChain ChatOpenAI).")
    p.add_argument("--max-context-tokens", type=int, default=10_000, help="Token budget for retrieved context.")
    p.add_argument("--max-chunks", type=int, default=40, help="Maximum chunks to include in context.")
    p.add_argument("--verbose", action="store_true", help="Enable debug logs.")
    p.add_argument("--output-json", action="store_true", help="Print raw JSON output per address.")
    return p


def main() -> None:
    load_dotenv()
    args = build_arg_parser().parse_args()
    _configure_logging(args.verbose)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set.")

    loader = ZoningDataLoader(args.data_dir)
    parser = MarkdownParser()

    context_builder = ContextBuilder(
        parser=parser,
        cfg=ContextBuilderConfig(
            max_context_tokens=args.max_context_tokens,
            max_chunks=args.max_chunks,
            model_for_token_estimation=args.model,
        ),
    )

    analyzer = ZoningAnalyzer(api_key=api_key, model=args.model, temperature=0.0)

    plan = ResidentPlan(
        structure="bijbehorend bouwwerk (outbuilding)",
        area_m2=20.0,
        height_m=3.0,
        intended_use="Living space (verblijfsgebied), subordinate to the main house",
    )

    filenames: List[str]
    if args.files:
        filenames = list(args.files)
    else:
        filenames = [f for f in loader.iter_json_files() if f.endswith(".json")]

    print(f"Starting zoning analysis for {len(filenames)} addresses...\n")

    for filename in filenames:
        zoning_file = loader.load_file(filename)
        address = zoning_file.address.display_address
        print(f"--- Processing: {filename} ---")
        print(f"Address: {address}")
        print(f"Bestemmingsvlakken: {zoning_file.zoning_metadata.bestemmingsvlakken}")

        valid_docs = loader.filter_documents(zoning_file.zoning_documents)
        print(f"Documents considered: {len(valid_docs)} (after Parapluplan/type filtering)")

        zoning_context, selected_chunks = context_builder.build_context(
            zoning_plan=zoning_file,
            documents=valid_docs,
            plan=plan,
        )

        print(f"Retrieved context size: {len(zoning_context)} chars")
        print("Analyzing with LLM...")

        try:
            assessment = analyzer.analyze(
                plan=plan,
                zoning_context=zoning_context,
                metadata=zoning_file.zoning_metadata,
                address=address,
            )

            if args.output_json:
                print(json.dumps(assessment.model_dump(), ensure_ascii=False, indent=2))
            else:
                print(f"Decision (permit-free): {assessment.permit_free.value}")
                print(f"Summary: {assessment.summary}")
                if assessment.suggested_changes:
                    print(f"Suggested changes: {assessment.suggested_changes}")
                if assessment.missing_information:
                    print("Missing information:")
                    for mi in assessment.missing_information:
                        print(f" - {mi}")
                if assessment.risk_flags:
                    print("Risk flags:")
                    for rf in assessment.risk_flags:
                        print(f" - {rf}")

                print("Evidence:")
                for ev in assessment.cited_evidence:
                    art = ev.article or "N/A"
                    print(f" - {ev.source_document} | Artikel {art}: {ev.excerpt} ({ev.relevance})")

        except Exception as e:
            print(f"ERROR analyzing {filename}: {e}")

        print("-" * 80 + "\n")


if __name__ == "__main__":
    main()
