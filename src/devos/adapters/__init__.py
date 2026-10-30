"""Source plugins, discovered lazily through installed entry-point metadata."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from devos.core import DevosError
from devos.ports import Adapter


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}
        self.unavailable: set[str] = set()
        try:
            entries = list(entry_points(group="devos.adapters"))
        except Exception:
            entries = []
        for entry in entries:
            if entry.name in {"github", "arxiv", "huggingface"}:
                continue  # Installed plugins cannot shadow or disable the built-in adapter.
            if entry.name in self._entries:
                self.unavailable.add(entry.name)
            else:
                self._entries[entry.name] = entry

    def load(self, key: str) -> Adapter:
        try:
            if key in self.unavailable:
                raise DevosError("plugin_unavailable", "Duplicate adapter entry-point key.")
            if key == "github":
                from devos.adapters.github import GitHubAdapter
                adapter: Adapter = GitHubAdapter()
            elif key == "arxiv":
                from devos.adapters.arxiv import ArxivAdapter
                adapter = ArxivAdapter()
            elif key == "huggingface":
                from devos.adapters.huggingface import HuggingFaceAdapter
                adapter = HuggingFaceAdapter()
            else:
                adapter = self._entries[key].load()()
            descriptor = adapter.descriptor
            if descriptor.key != key or descriptor.contract_major != 1:
                raise ValueError("incompatible contract")
            return adapter
        except Exception:
            raise DevosError("plugin_unavailable", "Selected adapter is missing, incompatible, or failed to load.") from None

    def select(self, value: str, explicit: str | None = None) -> Adapter:
        if explicit:
            return self.load(explicit)
        # Only built-ins auto-match. External packages must be explicitly selected;
        # inspecting a URL must not import every installed plugin.
        matches = [(adapter.match(value), adapter) for adapter in
                   (self.load("arxiv"), self.load("huggingface"), self.load("github"))]
        matches = [match for match in matches if match[0] > 0]
        if matches:
            return max(matches, key=lambda match: match[0])[1]
        raise DevosError("unsupported_input", "No built-in adapter recognizes this input; select --adapter.")
