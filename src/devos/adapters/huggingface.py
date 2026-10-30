"""Public Hugging Face models/datasets, cards, and explicit cross-references."""

import json
from urllib.parse import quote

from devos.adapters.references import hf_reference, hf_resource, paper_reference, text_references
from devos.core import Batch, Descriptor, DevosError, Fact, Scope, Target
from devos.ports import Document, Fetcher


def strings(value: object) -> list[str]:
    return [value] if isinstance(value, str) else [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


class HuggingFaceAdapter:
    descriptor = Descriptor("huggingface", "1.0.0", input_kinds=("model_url", "dataset_url", "hf_id"),
                            output_types=("hf_model", "hf_dataset"), credential_env="HF_TOKEN",
                            allowed_hosts=("huggingface.co",))

    def match(self, value: str) -> int:
        if not value.startswith(("hf:", "huggingface:", "https://huggingface.co/", "http://huggingface.co/", "https://www.huggingface.co/")):
            return 0
        try:
            hf_resource(value)
            return 120
        except DevosError:
            return 0

    def discover(self, value: str) -> Target:
        kind, repo_id = hf_resource(value)
        return Target(f"{kind}/{repo_id}", f"https://huggingface.co/{'datasets/' if kind == 'dataset' else ''}{repo_id}")

    @staticmethod
    def parse(document: Document) -> dict:
        try:
            raw = json.loads(document.body)
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError
            if raw.get("private") is True:
                raise DevosError("access_denied", "The Hugging Face adapter currently imports public repositories only.")
            return raw
        except (ValueError, UnicodeError):
            raise DevosError("invalid_payload", "Hugging Face returned malformed repository metadata.") from None

    def normalize(self, target: Target, document: Document, raw: dict, card: Document | None = None,
                  warning: str | None = None) -> Batch:
        kind, _ = target.resource_key.split("/", 1)
        canonical_ref = hf_reference(raw["id"], kind, "", {}, "mentions")
        scope = f"hf_{kind}.metadata"
        raw_card = raw.get("cardData")
        card_data: dict = raw_card if isinstance(raw_card, dict) else {}
        metadata = {key: raw.get(key) for key in ("author", "pipeline_tag", "library_name", "tags", "downloads", "likes", "sha", "lastModified", "gated")}
        metadata["card_data"] = card_data
        facts = [Fact("name", raw["id"], scope, {"json_pointer": "/id"}),
                 Fact("canonical_url", canonical_ref.url, scope, {"json_pointer": "/id"}),
                 Fact("metadata", metadata, scope, {"json_pointer": "/"})]
        description = raw.get("description") or card_data.get("description")
        if isinstance(description, str) and description.strip():
            facts.append(Fact("description", description.strip(), scope, {"json_pointer": "/description|cardData/description"}))
        scopes = [Scope(scope, document.snapshot_id, True, document.freshness)]
        refs = []
        warnings = [warning] if warning else []
        if kind == "model":
            relation_kind = card_data.get("base_model_relation")
            relation = "fine_tuned_from" if relation_kind == "finetune" else "derived_from"
            for field, ref_kind, rel in (("base_model", "model", relation), ("datasets", "dataset", "uses_dataset")):
                for value in strings(card_data.get(field))[:100]:
                    try:
                        refs.append(hf_reference(value, ref_kind, scope, {"json_pointer": f"/cardData/{field}"}, rel))
                    except DevosError:
                        warnings.append(f"Skipped invalid {field} reference.")
        for tag in strings(raw.get("tags")):
            if tag.startswith("arxiv:"):
                try:
                    refs.append(paper_reference(tag, scope, {"json_pointer": "/tags", "tag": tag}))
                except DevosError:
                    warnings.append("Skipped invalid arXiv tag.")
        if card is not None:
            try:
                text = card.body.decode("utf-8")
                card_scope = f"hf_{kind}.card"
                facts.append(Fact("readme", text, card_scope, {"path": "README.md"}))
                scopes.append(Scope(card_scope, card.snapshot_id, True, card.freshness))
                refs.extend(text_references(text, card_scope, locator={"path": "README.md"}))
            except UnicodeError:
                warnings.append("Card is not valid UTF-8; earlier card claims were retained.")
        identifiers = list(canonical_ref.identifiers)
        identifiers.append((f"huggingface:{kind}", target.resource_key.split("/", 1)[1].lower(), "alias"))
        identifiers.append(("url", target.url, "alias"))
        if isinstance(raw.get("_id"), str):
            identifiers.insert(0, (f"huggingface:{kind}:id", raw["_id"], "authoritative"))
        unique_refs = {(ref.scope, ref.relation, ref.url): ref for ref in refs if ref.url != canonical_ref.url}
        return Batch(f"hf_{kind}", tuple(dict.fromkeys(identifiers)), tuple(facts), tuple(scopes),
                     tuple(warnings), tuple(list(unique_refs.values())[:200]))

    def extract(self, target: Target, fetcher: Fetcher) -> Batch:
        kind, repo_id = target.resource_key.split("/", 1)
        document = fetcher.fetch(target.resource_key + "/metadata", f"https://huggingface.co/api/{kind}s/{quote(repo_id, safe='/')}")
        raw = self.parse(document)
        canonical_ref = hf_reference(raw["id"], kind, "", {}, "mentions")
        card, warning = None, None
        try:
            # raw/main serves text directly on huggingface.co; no model weights are downloaded.
            card = fetcher.fetch(target.resource_key + "/card", canonical_ref.url + "/raw/main/README.md")
        except DevosError as exc:
            warning = f"hf_{kind}.card: {exc.code}"
        return self.normalize(target, document, raw, card, warning)
