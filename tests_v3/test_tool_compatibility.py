from __future__ import annotations

import core_tools
from yueyue_v3.observations import ObservationService
from yueyue_v3.tools import ToolCatalogV3


def test_v3_preserves_every_legacy_public_tool_name(runtime_root) -> None:
    catalog = ToolCatalogV3(ObservationService(runtime_root / "workspace" / "project_cache" / "observations"))
    expected = {tool.name for tool in core_tools.ALL_TOOLS}
    assert set(catalog.names()) == expected
    assert len(expected) == 30


def test_click_ui_element_schema_is_backward_compatible(runtime_root) -> None:
    catalog = ToolCatalogV3(ObservationService(runtime_root / "workspace" / "project_cache" / "observations"))
    schema = catalog.get("click_ui_element").parameters
    assert schema["required"] == ["element_id"]
    assert "snapshot_id" in schema["properties"]


def test_observation_service_keeps_large_structured_ui_snapshots(runtime_root) -> None:
    service = ObservationService(runtime_root / "workspace" / "project_cache" / "observations")
    assert service.max_ui_elements >= 1000


def test_workspace_prefixed_paths_resolve_to_the_same_file(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "brain" / "personality.md"
    target.parent.mkdir(parents=True)
    target.write_text("# YueYue", encoding="utf-8")
    monkeypatch.setattr(core_tools, "WORKSPACE_DIR", str(workspace))

    relative = core_tools.real_read_file("brain/personality.md")
    prefixed = core_tools.real_read_file("workspace/brain/personality.md")
    absolute = core_tools.real_read_file(str(target))

    assert relative.status == "ok"
    assert prefixed.status == "ok"
    assert absolute.status == "ok"
    assert relative.data == prefixed.data == absolute.data == "# YueYue"

    written = core_tools.real_write_file("workspace/generated.txt", "hello")
    listed = core_tools.real_list_files("workspace")
    deleted = core_tools.real_delete_file("workspace/generated.txt")

    assert written.status == "ok"
    assert written.data["path"] == str(workspace / "generated.txt")
    assert "generated.txt" in listed.data
    assert deleted.status == "ok"
    assert not (workspace / "workspace").exists()
