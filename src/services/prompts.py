"""PromptManager: system prompts live in /prompts/<name>/system.md (versioned by Git)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: str  # first 12 hex chars of the template sha256
    text: str


def untrusted(text: str, label: str = "data") -> str:
    """Fence external text (web pages, prospect messages) so prompts can tell data from instructions."""
    safe = text.replace("</untrusted_data>", "[/untrusted_data]")
    return f'<untrusted_data label="{label}">\n{safe}\n</untrusted_data>'


class PromptManager:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else PROMPTS_DIR

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise PromptError(f"invalid prompt name: {name!r}")
        return self.root / name / "system.md"

    def template(self, name: str) -> str:
        p = self._path(name)
        if not p.is_file():
            raise PromptError(f"prompt not found: {p}")
        return p.read_text(encoding="utf-8")

    def fragment(self, name: str, part: str) -> str:
        """Small prompt fragments that live next to an agent's system.md (e.g. prompts/sales/disclosure_text.md)."""
        if not re.fullmatch(r"[a-z0-9_]+", name) or not re.fullmatch(r"[a-z0-9_]+", part):
            raise PromptError("invalid fragment name")
        p = self.root / name / f"{part}.md"
        if not p.is_file():
            raise PromptError(f"prompt fragment not found: {p}")
        return p.read_text(encoding="utf-8").strip()

    def version(self, name: str) -> str:
        return hashlib.sha256(self.template(name).encode()).hexdigest()[:12]

    def variables(self, name: str) -> set[str]:
        return set(_VAR.findall(self.template(name)))

    def load(self, name: str, **variables: Any) -> RenderedPrompt:
        tpl = self.template(name)
        missing = set(_VAR.findall(tpl)) - set(variables)
        if missing:
            raise PromptError(f"prompt {name!r} missing variables: {sorted(missing)}")

        def sub(m: re.Match) -> str:
            v = variables[m.group(1)]
            if isinstance(v, str):
                return v
            return json.dumps(v, indent=2, default=str, ensure_ascii=False)

        # single pass: injected values are never re-scanned for {{...}}
        text = _VAR.sub(sub, tpl)
        return RenderedPrompt(name=name, version=hashlib.sha256(tpl.encode()).hexdigest()[:12], text=text)
