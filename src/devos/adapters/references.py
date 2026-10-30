"""Conservative source identifiers and references; no network or merging heuristics."""

import re
from urllib.parse import urlsplit

from devos.core import DevosError, Reference, github_slug, safe_url

ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v[1-9]\d*)?"


def arxiv_id(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(safe_url(value))
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise DevosError("unsupported_input", "Use an arXiv paper URL or identifier.")
        if not parsed.path.startswith(("/abs/", "/pdf/")):
            raise DevosError("unsupported_input", "Use an arXiv abstract or PDF URL.")
        value = parsed.path[5:].removesuffix(".pdf")
    value = re.sub(r"^arxiv:", "", value, flags=re.I)
    if not re.fullmatch(ARXIV_ID, value, flags=re.I):
        raise DevosError("unsupported_input", "Invalid arXiv identifier.")
    return value.lower()


def paper_reference(value: str, scope: str, locator: dict, relation: str = "cites") -> Reference:
    paper_id = re.sub(r"v\d+$", "", arxiv_id(value))
    url = f"https://arxiv.org/abs/{paper_id}"
    return Reference("paper", (("arxiv", paper_id, "authoritative"), ("url", url, "alias")),
                     f"arXiv:{paper_id}", url, relation, scope, locator)


def hf_resource(value: str, *, default_kind: str = "model") -> tuple[str, str]:
    value = value.strip()
    kind = default_kind
    if value.startswith(("hf:", "huggingface:")):
        value = value.split(":", 1)[1]
    if "://" in value:
        parsed = urlsplit(safe_url(value))
        if parsed.hostname not in {"huggingface.co", "www.huggingface.co"}:
            raise DevosError("unsupported_input", "Use a Hugging Face model or dataset URL.")
        value = parsed.path.strip("/")
    for prefix, resource_kind in (("datasets/", "dataset"), ("models/", "model")):
        if value.startswith(prefix):
            kind, value = resource_kind, value[len(prefix):]
            break
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)?", value):
        raise DevosError("unsupported_input", "Use a model or dataset repository ID, without a file path.")
    if value.split("/")[0] in {"spaces", "papers", "collections", "docs", "api"}:
        raise DevosError("unsupported_input", "This Hugging Face resource is not a model or dataset.")
    return kind, value


def hf_reference(value: str, kind: str, scope: str, locator: dict, relation: str) -> Reference:
    actual_kind, repo_id = hf_resource(value, default_kind=kind)
    if actual_kind != kind:
        raise DevosError("invalid_payload", "Hugging Face reference type does not match its field.")
    url = f"https://huggingface.co/{'datasets/' if kind == 'dataset' else ''}{repo_id}"
    return Reference(f"hf_{kind}", ((f"huggingface:{kind}", repo_id.lower(), "alias"), ("url", url, "alias")),
                     repo_id, url, relation, scope, locator)


def text_references(text: str, scope: str, *, locator: dict | None = None) -> tuple[Reference, ...]:
    refs: dict[tuple[str, str], Reference] = {}
    for match in re.finditer(r"https?://[^\s<>\]\[\)\(\"']+", text):
        url = match.group().rstrip(".,;:}")
        where = {**(locator or {}), "span": [match.start(), match.start() + len(url)], "url": url}
        try:
            host = urlsplit(url).hostname
            if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
                ref = paper_reference(url, scope, where)
            elif host in {"github.com", "www.github.com"}:
                parsed = urlsplit(safe_url(url))
                parts = parsed.path.strip("/").split("/")
                if len(parts) < 2:
                    continue
                slug = github_slug("/".join(parts[:2]))
                canonical = f"https://github.com/{slug}"
                ref = Reference("repository", (("url", canonical, "alias"),), slug, canonical, "mentions", scope, where)
            elif host in {"huggingface.co", "www.huggingface.co"}:
                kind, _ = hf_resource(url)
                ref = hf_reference(url, kind, scope, where, "mentions")
            else:
                continue
            refs[(ref.relation, ref.url)] = ref
            if len(refs) >= 100:
                break
        except DevosError:
            continue
    return tuple(refs.values())
