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


def inventory_subdir() -> str:
    """Name of the inventory subfolder (override with BRAIN_INVENTORY_DIR)."""
    return os.environ.get("BRAIN_INVENTORY_DIR", "inventory")


def inventory_dir() -> Path:
    """The inventory subfolder (override the name with BRAIN_INVENTORY_DIR)."""
    sub = inventory_subdir()
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


# A value safe to write as a bare (unquoted) YAML scalar.
_SAFE_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./&()+'-]*$")


def _quote(value: str) -> str:
    """Render a value as an always-quoted, escaped YAML double-quoted scalar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_scalar(value: str) -> str:
    """Plain YAML scalar when it's safe, otherwise a quoted/escaped one."""
    if _SAFE_PLAIN.match(value) and ": " not in value:
        return value
    return _quote(value)


def _wikilink(name: str) -> str:
    """A quoted ``[[name]]`` value, safe even if the name has odd characters."""
    return _quote(f"[[{_unwiki(name)}]]")


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


# Field weights for ranking: a hit in the name beats a hit in the body.
_FIELD_WEIGHTS = (("name", 8), ("category", 4), ("parent", 4), ("body", 1))

DEFAULT_SEARCH_LIMIT = 20


def _score(item: Item, terms: list[str]) -> int:
    """Rank an item against query terms. Returns 0 unless every term matches."""
    fields = {
        "name": (item.name or "").lower(),
        "category": (item.category or "").lower(),
        "parent": (item.parent or "").lower(),
        "body": (item.body or "").lower(),
    }
    total = 0
    for term in terms:
        best = 0
        for field, weight in _FIELD_WEIGHTS:
            hay = fields[field]
            if term not in hay:
                continue
            w = weight
            if hay == term:
                w += 4  # exact field match
            elif re.search(rf"\b{re.escape(term)}", hay):
                w += 1  # word-boundary (prefix) match
            best = max(best, w)
        if best == 0:
            return 0  # AND semantics: every term must hit some field
        total += best
    return total


def search_items(query: str, limit: int | None = DEFAULT_SEARCH_LIMIT) -> list[Item]:
    """Ranked, token-AND search over name/category/parent/body.

    The query is split into whitespace terms; an item matches only if *every*
    term appears somewhere. Results are ranked (name > category/parent > body)
    and capped at ``limit`` (pass ``None`` for no cap).
    """
    terms = [t for t in query.strip().lower().split() if t]
    if not terms:
        return []
    scored: list[tuple[int, str, Item]] = []
    for item in all_items():
        s = _score(item, terms)
        if s > 0:
            scored.append((s, item.name.lower(), item))
    scored.sort(key=lambda t: (-t[0], t[1]))
    items = [it for _, _, it in scored]
    return items[:limit] if limit else items


def _today() -> str:
    return date.today().isoformat()


def _atomic_write(path: Path, text: str) -> None:
    """Write a note crash-safely: write a temp file, then atomically replace.

    Avoids leaving a half-written note if the process dies mid-write.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _split_head_body(text: str) -> tuple[str, str]:
    """Split into (frontmatter block incl. closing ---, body). ('', text) if none."""
    if not text.startswith(FM_DELIM):
        return "", text
    end = text.index(FM_DELIM, len(FM_DELIM))
    head = text[:end] + FM_DELIM
    body = text[end + len(FM_DELIM):].lstrip("\n")
    return head, body


def _retitle_h1(body: str, old: str, new: str) -> str:
    """Rewrite a leading ``# old`` heading to ``# new`` (only the title line)."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.strip() == f"# {old}":
            lines[i] = f"# {new}"
        break  # only consider the first non-empty line
    return "\n".join(lines)


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
        f"name: {_quote(name)}",
        f"category: {_yaml_scalar(category)}",
        f"is_container: {'true' if is_container else 'false'}",
    ]
    if parent:
        lines.append(f"parent: {_wikilink(parent)}")
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
    _atomic_write(path, "\n".join(lines))
    return load_item(path)


def _set_fm_field(text: str, key: str, value: str) -> str:
    """Set or insert a single frontmatter scalar field, preserving the rest."""
    if not text.startswith(FM_DELIM):
        raise ValueError("File has no frontmatter to edit")
    end = text.index(FM_DELIM, len(FM_DELIM))
    head, rest = text[:end], text[end:]
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pattern.search(head):
        # function replacement -> value is used literally (no backslash/group magic)
        head = pattern.sub(lambda _m: f"{key}: {value}", head, count=1)
    else:
        head = head.rstrip("\n") + f"\n{key}: {value}\n"
    return head + rest


def move_item(name: str, new_parent: str) -> Item:
    item = find_item(name)
    if not item:
        raise FileNotFoundError(f"No inventory item named {name!r}")
    text = item.path.read_text(encoding="utf-8")
    text = _set_fm_field(text, "parent", _wikilink(new_parent))
    text = _set_fm_field(text, "updated", _today())
    _atomic_write(item.path, text)
    return load_item(item.path)


def _reparent_children(old_parent: str, new_parent: str) -> int:
    """Re-point every item whose parent was ``old_parent`` to ``new_parent``."""
    target = old_parent.strip().lower()
    count = 0
    for child in all_items():
        if (child.parent or "").lower() == target:
            t = child.path.read_text(encoding="utf-8")
            t = _set_fm_field(t, "parent", _wikilink(new_parent))
            t = _set_fm_field(t, "updated", _today())
            _atomic_write(child.path, t)
            count += 1
    return count


def update_item(
    name: str,
    category: str | None = None,
    append_body: str | None = None,
    replace_body: str | None = None,
    is_container: bool | None = None,
    new_name: str | None = None,
) -> Item:
    """Update an item's metadata and/or body.

    - ``category`` / ``is_container`` set those frontmatter fields.
    - ``append_body`` adds a line; ``replace_body`` overwrites the prose under the
      ``# Title`` heading (mutually exclusive with ``append_body``).
    - ``new_name`` renames the item: it rewrites the ``name`` field, the ``# Title``
      heading and the filename, and re-points any children that listed it as parent.
    """
    if append_body is not None and replace_body is not None:
        raise ValueError("Pass either append_body or replace_body, not both")
    item = find_item(name)
    if not item:
        raise FileNotFoundError(f"No inventory item named {name!r}")

    renaming = bool(new_name) and new_name != item.name
    if renaming:
        new_path = path_for(new_name)
        if new_path.exists() and new_path != item.path:
            raise FileExistsError(f"Item already exists: {new_path.name}")
    display = new_name if new_name else item.name

    text = item.path.read_text(encoding="utf-8")
    if new_name:
        text = _set_fm_field(text, "name", _quote(new_name))
    if category:
        text = _set_fm_field(text, "category", _yaml_scalar(category))
    if is_container is not None:
        text = _set_fm_field(text, "is_container", "true" if is_container else "false")

    if replace_body is not None:
        head, _ = _split_head_body(text)
        prose = replace_body.strip()
        text = head + f"\n\n# {display}\n\n" + (prose + "\n" if prose else "")
    elif new_name:
        head, body = _split_head_body(text)
        text = head + "\n\n" + _retitle_h1(body, item.name, new_name).lstrip("\n")

    if append_body and append_body.strip():
        text = text.rstrip("\n") + f"\n\n{append_body.strip()}\n"

    text = _set_fm_field(text, "updated", _today())

    if renaming:
        _atomic_write(new_path, text)
        item.path.unlink()
        _reparent_children(item.name, new_name)
        return load_item(new_path)
    _atomic_write(item.path, text)
    return load_item(item.path)


def remove_item(name: str) -> Item:
    """Delete an item's note from the vault. Returns the removed item."""
    item = find_item(name)
    if not item:
        raise FileNotFoundError(f"No inventory item named {name!r}")
    item.path.unlink()
    return item


def git_commit(message: str) -> str:
    """Stage inventory changes and commit+push. Best-effort; returns status text."""
    root = vault_root()
    sub = inventory_subdir()
    try:
        subprocess.run(
            ["git", "add", sub], cwd=root, check=True,
            capture_output=True, text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", sub], cwd=root,
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
