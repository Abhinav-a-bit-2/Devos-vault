"""Conservative direct dependency extraction from root repository manifests."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from devos.core import DevosError, Reference

MAX_MANIFEST_BYTES = 512_000
MAX_REFERENCES = 75
PYTHON_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")


def _body(body: bytes) -> str:
    if len(body) > MAX_MANIFEST_BYTES:
        raise DevosError("invalid_payload", "Dependency manifest exceeds the 512 KB limit.")
    try:
        return body.decode("utf-8-sig")
    except UnicodeError:
        raise DevosError("invalid_payload", "Dependency manifest must be UTF-8.") from None


def _reference(name: str, ecosystem: str, scope: str, locator: dict,
               qualifiers: dict) -> Reference:
    normalized = re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "pypi" else name.lower()
    url = (f"https://pypi.org/project/{quote(normalized)}/" if ecosystem == "pypi"
           else f"https://www.npmjs.com/package/{quote(normalized, safe='@/')}")
    return Reference("package", ((ecosystem, normalized, "alias"), ("url", url, "alias")),
                     name, url, "depends_on", scope, locator, qualifiers)


def requirements(body: bytes) -> tuple[Reference, ...]:
    """Read only direct PEP 508-like names/specifiers; omit URLs, includes and editable installs."""
    refs: list[Reference] = []
    for number, raw in enumerate(_body(body).splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http:", "https:")):
            continue
        match = PYTHON_NAME.match(line)
        if not match or len(match.group()) > 214:
            continue
        rest = line[match.end():].strip()
        if rest.startswith("["):
            end = rest.find("]")
            if end < 0:
                continue
            rest = rest[end + 1:].strip()
        if rest and not rest.startswith(("==", ">=", "<=", "~=", "!=", ">", "<", ";")):
            continue
        specifier = rest.split(";", 1)[0].strip()
        refs.append(_reference(match.group(), "pypi", "repository.requirements",
                               {"path": "requirements.txt", "line": number},
                               {"ecosystem": "pypi", "scope": "runtime", "specifier": specifier}))
        if len(refs) >= MAX_REFERENCES:
            break
    return tuple(refs)


def package_json(body: bytes) -> tuple[Reference, ...]:
    try:
        data = json.loads(_body(body))
    except ValueError:
        raise DevosError("invalid_payload", "package.json is not valid JSON.") from None
    if not isinstance(data, dict):
        raise DevosError("invalid_payload", "package.json must contain an object.")
    refs: list[Reference] = []
    for field, kind in (("dependencies", "runtime"), ("devDependencies", "development"),
                        ("optionalDependencies", "optional"), ("peerDependencies", "peer")):
        values = data.get(field, {})
        if not isinstance(values, dict):
            raise DevosError("invalid_payload", f"package.json {field} must be an object.")
        for name, specifier in values.items():
            if (not isinstance(name, str) or len(name) > 214 or not NPM_NAME.fullmatch(name)
                    or not isinstance(specifier, str) or not specifier.strip()):
                continue
            # Non-registry aliases and Git/URL/local references need separate identity rules.
            if specifier.startswith(("git+", "github:", "file:", "http:", "https:", "npm:",
                                     "workspace:", "link:", "portal:", "patch:")):
                continue
            refs.append(_reference(name, "npm", "repository.package_json",
                                   {"path": "package.json", "json_pointer": f"/{field}/{name}"},
                                   {"ecosystem": "npm", "scope": kind, "specifier": specifier[:200]}))
            if len(refs) >= MAX_REFERENCES:
                return tuple(refs)
    return tuple(refs)
