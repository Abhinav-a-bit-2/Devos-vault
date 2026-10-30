"""arXiv Atom metadata adapter. Paper versions share one canonical identity."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from devos.adapters.references import arxiv_id, text_references
from devos.core import Batch, Descriptor, DevosError, Fact, Scope, Target
from devos.ports import Document, Fetcher

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivAdapter:
    descriptor = Descriptor("arxiv", "1.0.0", input_kinds=("paper_url", "arxiv_id"),
                            output_types=("paper",), allowed_hosts=("export.arxiv.org",), min_interval=3)

    def match(self, value: str) -> int:
        try:
            arxiv_id(value)
            return 120
        except DevosError:
            return 0

    def discover(self, value: str) -> Target:
        identifier = arxiv_id(value)
        # A vault tracks the paper's latest metadata. Versions are recorded in metadata,
        # and all versioned input URLs resolve to the same paper.
        base = re.sub(r"v\d+$", "", identifier)
        return Target(base, f"https://arxiv.org/abs/{base}")

    @staticmethod
    def parse(document: Document) -> ET.Element:
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", document.body, re.I):
            raise DevosError("invalid_payload", "DTD/entity declarations are not permitted in arXiv XML.")
        try:
            root = ET.fromstring(document.body)
        except ET.ParseError:
            raise DevosError("invalid_payload", "arXiv returned malformed XML.") from None
        entries = root.findall(f"{ATOM}entry")
        if len(entries) != 1:
            raise DevosError("not_found", "arXiv did not return the requested paper.")
        return entries[0]

    def normalize(self, target: Target, document: Document, entry: ET.Element) -> Batch:
        def text(tag: str) -> str:
            return " ".join((entry.findtext(tag) or "").split())

        try:
            versioned = arxiv_id(text(f"{ATOM}id"))
        except DevosError:
            raise DevosError("invalid_payload", "arXiv returned an error entry or invalid paper ID.") from None
        if re.sub(r"v\d+$", "", versioned) != target.resource_key:
            raise DevosError("invalid_payload", "arXiv returned a different paper than requested.")
        title, abstract = text(f"{ATOM}title"), text(f"{ATOM}summary")
        if not title or not abstract:
            raise DevosError("invalid_payload", "arXiv paper title or abstract is missing.")
        metadata = {"arxiv_id": target.resource_key, "version": versioned,
                    "authors": [" ".join((a.findtext(f"{ATOM}name") or "").split()) for a in entry.findall(f"{ATOM}author")],
                    "published": text(f"{ATOM}published"), "updated": text(f"{ATOM}updated"),
                    "categories": [c.get("term") for c in entry.findall(f"{ATOM}category")],
                    "doi": text(f"{ARXIV}doi"), "comment": text(f"{ARXIV}comment"),
                    "journal_ref": text(f"{ARXIV}journal_ref")}
        scope = "paper.metadata"
        facts = tuple(Fact(key, value, scope, {"xml_path": path}) for key, value, path in (
            ("name", title, "entry/title"), ("description", abstract, "entry/summary"),
            ("canonical_url", target.url, "entry/id"), ("metadata", metadata, "entry"),
        ))
        identifiers = [("arxiv", target.resource_key, "authoritative"), ("url", target.url, "alias")]
        if metadata["doi"]:
            identifiers.append(("doi", str(metadata["doi"]).lower(), "authoritative"))
        refs = text_references(abstract + "\n" + str(metadata["comment"]), scope, locator={"xml_path": "entry/summary|comment"})
        refs = tuple(ref for ref in refs if ref.url != target.url)
        return Batch("paper", tuple(identifiers), facts,
                     (Scope(scope, document.snapshot_id, True, document.freshness),), references=refs)

    def extract(self, target: Target, fetcher: Fetcher) -> Batch:
        document = fetcher.fetch(target.resource_key + "/metadata",
                                 "https://export.arxiv.org/api/query?id_list=" + quote(target.resource_key, safe="") + "&max_results=1")
        return self.normalize(target, document, self.parse(document))
