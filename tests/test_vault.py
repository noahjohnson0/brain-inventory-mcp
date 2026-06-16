"""Unit tests for the vault storage layer against a throwaway temp vault.

These exercise CRUD, ranked search, frontmatter parsing/editing, rename with
child re-parenting, and removal — no MCP SDK or OAuth involved.
"""

from __future__ import annotations

import pytest

from brain_inventory_mcp import vault


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    """A temp Obsidian vault with an empty inventory/ folder, wired via env."""
    (tmp_path / "inventory").mkdir()
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    monkeypatch.delenv("BRAIN_INVENTORY_DIR", raising=False)
    return tmp_path


def test_create_and_get_roundtrip(vault_dir):
    item = vault.create_item(
        name="Anker Nano Charger",
        category="Electronics",
        parent="Desk",
        body="GaN desk charger.",
        is_container=False,
    )
    assert item.name == "Anker Nano Charger"
    assert item.category == "Electronics"
    assert item.parent == "Desk"  # wikilink brackets stripped
    assert item.is_container is False
    assert item.path.name == "Anker Nano Charger.md"

    fetched = vault.find_item("anker nano charger")  # case-insensitive
    assert fetched is not None
    assert fetched.name == "Anker Nano Charger"
    assert "GaN desk charger." in fetched.body
    # frontmatter stamped both dates
    assert fetched.meta["created"] == fetched.meta["updated"]


def test_create_duplicate_raises(vault_dir):
    vault.create_item(name="Passport")
    with pytest.raises(FileExistsError):
        vault.create_item(name="Passport")


def test_illegal_filename_chars_stripped(vault_dir):
    item = vault.create_item(name='Cable: USB-C/USB-A "fast"')
    assert ":" not in item.path.name and "/" not in item.path.name
    assert item.path.is_file()
    # the canonical name in frontmatter is preserved verbatim
    assert item.name == 'Cable: USB-C/USB-A "fast"'


def test_list_filters_by_category_and_parent(vault_dir):
    vault.create_item(name="Hammer", category="Tools", parent="Garage")
    vault.create_item(name="Drill", category="Tools", parent="Garage")
    vault.create_item(name="Spoon", category="Kitchen", parent="Drawer")

    assert {i.name for i in vault.list_items(category="tools")} == {"Hammer", "Drill"}
    assert {i.name for i in vault.list_items(parent="[[Drawer]]")} == {"Spoon"}
    assert {i.name for i in vault.list_items(category="Tools", parent="Garage")} == {
        "Hammer",
        "Drill",
    }
    assert len(vault.list_items()) == 3


def test_search_ranks_name_over_body(vault_dir):
    vault.create_item(name="USB Cable", category="Electronics")
    vault.create_item(name="Notebook", category="Office", body="kept next to the usb cable")
    results = vault.search_items("usb cable")
    assert [r.name for r in results] == ["USB Cable", "Notebook"]


def test_search_requires_all_terms(vault_dir):
    vault.create_item(name="Blue Backpack", category="Bags")
    vault.create_item(name="Red Backpack", category="Bags")
    assert [r.name for r in vault.search_items("blue backpack")] == ["Blue Backpack"]
    # a term present in nothing eliminates the match
    assert vault.search_items("blue suitcase") == []


def test_search_handles_missing_fields(vault_dir):
    # An item with no parent/category must not match the literal word "none".
    vault.create_item(name="Lonely Item")
    assert vault.search_items("none") == []


def test_search_limit(vault_dir):
    for i in range(5):
        vault.create_item(name=f"Widget {i}", category="Widgets")
    assert len(vault.search_items("widget", limit=2)) == 2
    assert len(vault.search_items("widget", limit=None)) == 5


def test_move_updates_parent(vault_dir):
    vault.create_item(name="Mug", parent="Cupboard")
    moved = vault.move_item("Mug", "Dishwasher")
    assert moved.parent == "Dishwasher"
    assert vault.find_item("Mug").parent == "Dishwasher"


def test_update_category_and_append_body(vault_dir):
    vault.create_item(name="Lamp", category="Uncategorized", body="Original note.")
    updated = vault.update_item("Lamp", category="Lighting", append_body="Bought 2026.")
    assert updated.category == "Lighting"
    assert "Original note." in updated.body
    assert "Bought 2026." in updated.body


def test_update_replace_body_keeps_heading(vault_dir):
    vault.create_item(name="Notes", body="old prose")
    updated = vault.update_item("Notes", replace_body="brand new prose")
    assert "old prose" not in updated.body
    assert "brand new prose" in updated.body
    assert updated.body.lstrip().startswith("# Notes")


def test_append_and_replace_body_conflict(vault_dir):
    vault.create_item(name="Thing")
    with pytest.raises(ValueError):
        vault.update_item("Thing", append_body="a", replace_body="b")


def test_update_set_is_container(vault_dir):
    vault.create_item(name="Box")
    assert vault.update_item("Box", is_container=True).is_container is True
    assert vault.update_item("Box", is_container=False).is_container is False


def test_rename_moves_file_retitles_and_reparents_children(vault_dir):
    vault.create_item(name="Old Drawer", is_container=True)
    vault.create_item(name="Pen", parent="Old Drawer")
    vault.create_item(name="Pencil", parent="Old Drawer")

    renamed = vault.update_item("Old Drawer", new_name="New Drawer")
    assert renamed.name == "New Drawer"
    assert renamed.path.name == "New Drawer.md"
    assert renamed.body.lstrip().startswith("# New Drawer")

    # old file is gone, children now point at the new name
    assert vault.find_item("Old Drawer") is None
    assert vault.find_item("Pen").parent == "New Drawer"
    assert vault.find_item("Pencil").parent == "New Drawer"


def test_rename_onto_existing_raises(vault_dir):
    vault.create_item(name="A")
    vault.create_item(name="B")
    with pytest.raises(FileExistsError):
        vault.update_item("A", new_name="B")


def test_special_chars_roundtrip_through_rename_and_category(vault_dir):
    vault.create_item(name="Plain")
    # rename to a name with YAML-hostile characters; canonical name must survive
    renamed = vault.update_item(
        "Plain", new_name='Box: "spare" parts', category="A/V: cables"
    )
    assert renamed.name == 'Box: "spare" parts'
    assert renamed.category == "A/V: cables"
    # reload from disk to prove the frontmatter actually parses back
    again = vault.find_item('Box: "spare" parts')
    assert again is not None
    assert again.name == 'Box: "spare" parts'
    assert again.category == "A/V: cables"


def test_remove_item(vault_dir):
    vault.create_item(name="Trash Item")
    removed = vault.remove_item("Trash Item")
    assert removed.name == "Trash Item"
    assert vault.find_item("Trash Item") is None


def test_missing_item_errors(vault_dir):
    with pytest.raises(FileNotFoundError):
        vault.move_item("Ghost", "Nowhere")
    with pytest.raises(FileNotFoundError):
        vault.update_item("Ghost", category="X")
    with pytest.raises(FileNotFoundError):
        vault.remove_item("Ghost")


def test_split_frontmatter_tolerates_no_frontmatter():
    meta, body = vault.split_frontmatter("# Just a heading\n\ntext")
    assert meta == {}
    assert body.startswith("# Just a heading")


def test_custom_inventory_dir(tmp_path, monkeypatch):
    (tmp_path / "stuff").mkdir()
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    monkeypatch.setenv("BRAIN_INVENTORY_DIR", "stuff")
    assert vault.inventory_subdir() == "stuff"
    item = vault.create_item(name="Thing")
    assert item.path.parent.name == "stuff"
    assert vault.find_item("Thing") is not None
