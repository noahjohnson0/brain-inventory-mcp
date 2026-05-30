"""Vault access layer for the brain inventory.

Reads and writes the Markdown notes under ``<vault>/inventory``. Notes carry YAML
frontmatter (name, category, is_container, parent, created, updated, tags) followed
by a Markdown body. We parse frontmatter with PyYAML for reads, but mutate existing
files with targeted line edits so the human formatting/quoting is preserved.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

FM_DELIM = "---"


def vault_root() -> Path:
    """Resolve the Obsidian vault root from the BRAIN_VAULT env var."""
    raw = os.environ.get("BRAIN_VAULT", "")
    if not raw:
        raise FileNotFoundError(
            "Set BRAIN_VAULT to your Obsidian vault root, e.g. "
            "BRAIN_VAULT='~/Documents/Obsidian/my-vault'."
        )
    root = Path(os.path.expanduser(raw))
    if not root.is_dir():
        raise FileNotFoundError(f"Vault not found at {root!s}. Check BRAIN_VAULT.")
    return root


def inventory_dir() -> Path:
    """The inventory subfolder (override the name with BRAIN_INVENTORY_DIR)."""
    sub = os.environ.get("BRAIN_INVENTORY_DIR", "inventory")
    d = vault_root() / sub
    if not d.is_dir():
        raise FileNotFoundError(f"No {sub}/ dir under {vault_root()!s}")
    return d


@dataclass
class Item:
    name: str          # canonical name (from frontmatter, else filename stem)
    path: Path
    meta: dict
    body: str

    @property
    def category(self) -> str | None:
        return self.meta.get("category")

    @property
    def is_container(self) -> bool:
        return bool(self.meta.get("is_container"))

    @property
    def parent(self) -> str | None:
        """Parent as a plain name, wikilink brackets stripped."""
        return _unwiki(self.meta.get("parent"))

    def summary(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "is_container": self.is_container,
            "parent": self.parent,
            "file": self.path.name,
        }


def _unwiki(value) -> str | None:
    if not value:
        return None
    return re.sub(r"^\[\[(.*)\]\]$", r"\1", str(value).strip()).strip()


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body). Tolerates files with no frontmatter."""
    if not text.startswith(FM_DELIM):
        return {}, text
    parts = text.split(FM_DELIM, 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    body = parts[2].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def _stem_to_name(path: Path) -> str:
    return path.stem


def load_item(path: Path) -> Item:
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    name = str(meta.get("name") or _stem_to_name(path))
    return Item(name=name, path=path, meta=meta, body=body)


def _slug(name: str) -> str:
    """Filename for an item: Proper Case With Spaces, brand quirks preserved.

    We do not transform case here — callers pass the name as it should appear.
    We only strip characters illegal in filenames.
    """
    cleaned = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return cleaned


def path_for(name: str) -> Path:
    return inventory_dir() / f"{_slug(name)}.md"


def find_item(name: str) -> Item | None:
    """Find by exact filename, then by case-insensitive name/stem match."""
    direct = path_for(name)
    if direct.is_file():
        return load_item(direct)
    target = name.strip().lower()
    for p in inventory_dir().glob("*.md"):
        item = load_item(p)
        if item.name.lower() == target or p.stem.lower() == target:
            return item
    return None


def all_items() -> list[Item]:
    return [load_item(p) for p in sorted(inventory_dir().glob("*.md"))]


def list_items(category: str | None = None, parent: str | None = None) -> list[Item]:
    items = all_items()
    if category:
        c = category.strip().lower()
        items = [i for i in items if (i.category or "").lower() == c]
    if parent:
        pp = _unwiki(parent).lower() if _unwiki(parent) else parent.lower()
        items = [i for i in items if (i.parent or "").lower() == pp]
    return items


def search_items(query: str) -> list[Item]:
    q = query.strip().lower()
    hits = []
    for item in all_items():
        haystack = f"{item.name}\n{item.category}\n{item.parent}\n{item.body}".lower()
        if q in haystack:
            hits.append(item)
    return hits


def _today() -> str:
    return date.today().isoformat()


def create_item(
    name: str,
    category: str = "Uncategorized",
    parent: str | None = None,
    body: str = "",
    is_container: bool = False,
) -> Item:
    path = path_for(name)
    if path.exists():
        raise FileExistsError(f"Item already exists: {path.name}")
    today = _today()
    lines = [
        FM_DELIM,
        f'name: "{name}"',
        f"category: {category}",
        f"is_container: {'true' if is_container else 'false'}",
    ]
    if parent:
        lines.append(f'parent: "[[{_unwiki(parent)}]]"')
    lines += [
        f"created: {today}",
        f"updated: {today}",
        "tags: [inventory]",
        FM_DELIM,
        "",
        f"# {name}",
        "",
    ]
    if body.strip():
        lines.append(body.strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return load_item(path)


def _set_fm_field(text: str, key: str, value: str) -> str:
    """Set or insert a single frontmatter scalar field, preserving the rest."""
    if not text.startswith(FM_DELIM):
        raise ValueError("File has no frontmatter to edit")
    end = text.index(FM_DELIM, len(FM_DELIM))
    head, rest = text[:end], text[end:]
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pattern.search(head):
        head = pattern.sub(f"{key}: {value}", head, count=1)
    else:
        head = head.rstrip("\n") + f"\n{key}: {value}\n"
    return head + rest


def move_item(name: str, new_parent: str) -> Item:
    item = find_item(name)
    if not item:
        raise FileNotFoundError(f"No inventory item named {name!r}")
    text = item.path.read_text(encoding="utf-8")
    text = _set_fm_field(text, "parent", f'"[[{_unwiki(new_parent)}]]"')
    text = _set_fm_field(text, "updated", _today())
    item.path.write_text(text, encoding="utf-8")
    return load_item(item.path)


def update_item(
    name: str,
    category: str | None = None,
    append_body: str | None = None,
) -> Item:
    item = find_item(name)
    if not item:
        raise FileNotFoundError(f"No inventory item named {name!r}")
    text = item.path.read_text(encoding="utf-8")
    if category:
        text = _set_fm_field(text, "category", category)
    if append_body and append_body.strip():
        text = text.rstrip("\n") + f"\n\n{append_body.strip()}\n"
    text = _set_fm_field(text, "updated", _today())
    item.path.write_text(text, encoding="utf-8")
    return load_item(item.path)


def git_commit(message: str) -> str:
    """Stage inventory changes and commit+push. Best-effort; returns status text."""
    root = vault_root()
    try:
        subprocess.run(
            ["git", "add", "inventory"], cwd=root, check=True,
            capture_output=True, text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "inventory"], cwd=root,
            capture_output=True, text=True,
        )
        if not status.stdout.strip():
            return "no changes to commit"
        subprocess.run(
            ["git", "commit", "-m", message], cwd=root, check=True,
            capture_output=True, text=True,
        )
        push = subprocess.run(
            ["git", "push"], cwd=root, capture_output=True, text=True,
        )
        return "committed and pushed" if push.returncode == 0 else "committed (push failed)"
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.strip() or e}"
