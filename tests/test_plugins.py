from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from braincode.config import PluginConfig, load_config
from braincode.tools import ToolRegistry
from braincode.tools.plugins import PluginLoadError, PluginManifest, load_plugins


def _write_plugin(tmp_path: Path, body: str, name: str = "plugin.py") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _valid_plugin_body(tool_name: str = "PluginTool") -> str:
    return f'''\
        from pydantic import BaseModel
        from braincode.tools.base import Tool, ToolResult

        class Params(BaseModel):
            value: str = "ok"

        class T(Tool):
            name = "{tool_name}"
            description = "plugin tool"
            params_model = Params
            category = "read"

            async def execute(self, params):
                return ToolResult(output=params.value)

        class Plugin:
            manifest = {{"id": "sample-plugin", "version": "1.0.0"}}

            def create_tools(self, context):
                return [T()]

        def create_plugin(context):
            return Plugin()
    '''


def test_load_local_plugin_and_record_owner(tmp_path: Path) -> None:
    plugin_file = _write_plugin(tmp_path, _valid_plugin_body())
    registry = ToolRegistry()

    report = load_plugins(
        registry,
        work_dir=tmp_path,
        plugin_config=PluginConfig(
            entry_points=False,
            paths=[str(plugin_file)],
        ),
    )

    assert report.loaded == ["sample-plugin"]
    assert report.errors == []
    assert registry.get("PluginTool") is not None
    assert registry.get_tool_owner("PluginTool") == "sample-plugin"
    assert [tool.name for tool in registry.get_plugin_tools("sample-plugin")] == [
        "PluginTool"
    ]


def test_allow_and_deny_filter_plugins(tmp_path: Path) -> None:
    plugin_file = _write_plugin(tmp_path, _valid_plugin_body())
    registry = ToolRegistry()
    report = load_plugins(
        registry,
        work_dir=tmp_path,
        plugin_config=PluginConfig(
            entry_points=False,
            paths=[str(plugin_file)],
            allow=["other-plugin"],
        ),
    )

    assert report.loaded == []
    assert report.skipped == ["sample-plugin"]
    assert registry.get("PluginTool") is None

    registry = ToolRegistry()
    report = load_plugins(
        registry,
        work_dir=tmp_path,
        plugin_config=PluginConfig(
            entry_points=False,
            paths=[str(plugin_file)],
            deny=["sample-plugin"],
        ),
    )
    assert report.skipped == ["sample-plugin"]
    assert registry.get("PluginTool") is None


def test_plugin_conflict_isolated_and_strict_mode_raises(tmp_path: Path) -> None:
    plugin_file = _write_plugin(tmp_path, _valid_plugin_body("ReadFile"))
    registry = ToolRegistry()
    from braincode.tools.read_file import ReadFile

    registry.register(ReadFile())
    report = load_plugins(
        registry,
        work_dir=tmp_path,
        plugin_config=PluginConfig(entry_points=False, paths=[str(plugin_file)]),
    )
    assert report.loaded == []
    assert any("already registered" in error for error in report.errors)
    assert registry.get("ReadFile") is not None

    strict_registry = ToolRegistry()
    strict_registry.register(ReadFile())
    with pytest.raises(PluginLoadError):
        load_plugins(
            strict_registry,
            work_dir=tmp_path,
            plugin_config=PluginConfig(
                entry_points=False,
                paths=[str(plugin_file)],
                strict=True,
            ),
        )


def test_plugin_manifest_validation() -> None:
    manifest = PluginManifest.from_value({"id": "demo", "version": "1.0"})
    assert manifest.plugin_id == "demo"
    with pytest.raises(Exception):
        PluginManifest.from_value({"id": "Bad ID", "version": "1.0"})


def test_plugin_config_defaults_and_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
        providers:
          - name: test
            protocol: openai
            base_url: http://localhost/v1
            model: gpt-test
        plugins:
          enabled: false
          paths: [custom/plugins]
          allow: [sample-plugin]
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.plugins.enabled is False
    assert config.plugins.entry_points is True
    assert config.plugins.paths == ["custom/plugins"]
    assert config.plugins.allow == ["sample-plugin"]
