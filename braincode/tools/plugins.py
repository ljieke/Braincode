"""Trusted Python tool plugin discovery and loading.

Plugins are deliberately explicit in the MVP: they must come from a configured
file/directory or from the ``braincode.tools`` Python entry-point group. This
module does not sandbox Python code; loading a plugin means trusting its code.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Protocol

from braincode.tools.base import Tool

PLUGIN_API_VERSION = "1"
ENTRY_POINT_GROUP = "braincode.tools"
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PluginError(Exception):
    """Base exception for plugin discovery and loading errors."""


class PluginManifestError(PluginError):
    pass


class PluginLoadError(PluginError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    api_version: str = PLUGIN_API_VERSION
    namespace: str = ""
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "PluginManifest":
        if isinstance(value, cls):
            manifest = value
        elif isinstance(value, dict):
            capabilities = value.get("capabilities", ())
            if isinstance(capabilities, str):
                capabilities = (capabilities,)
            elif isinstance(capabilities, (list, tuple)):
                capabilities = tuple(str(item) for item in capabilities)
            else:
                raise PluginManifestError("manifest.capabilities must be a list")
            manifest = cls(
                plugin_id=str(value.get("id", value.get("plugin_id", ""))),
                version=str(value.get("version", "")),
                api_version=str(value.get("api_version", PLUGIN_API_VERSION)),
                namespace=str(value.get("namespace", "")),
                capabilities=capabilities,
            )
        else:
            raise PluginManifestError("plugin manifest must be a PluginManifest or mapping")

        if not _PLUGIN_ID_RE.fullmatch(manifest.plugin_id):
            raise PluginManifestError(
                "manifest.id must match ^[a-z0-9][a-z0-9._-]*$"
            )
        if not manifest.version.strip():
            raise PluginManifestError("manifest.version must not be empty")
        if manifest.api_version != PLUGIN_API_VERSION:
            raise PluginManifestError(
                f"unsupported plugin API version: {manifest.api_version}"
            )
        if manifest.namespace and not _PLUGIN_ID_RE.fullmatch(manifest.namespace):
            raise PluginManifestError("manifest.namespace contains invalid characters")
        return manifest


@dataclass
class ToolContext:
    """Dependencies exposed to trusted plugin factories."""

    work_dir: Path
    config: Any = None
    permission_checker: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("braincode.plugin"))
    services: dict[str, Any] = field(default_factory=dict)


class ToolPlugin(Protocol):
    manifest: PluginManifest | dict[str, Any]

    def create_tools(self, context: ToolContext) -> Iterable[Tool]: ...


@dataclass
class PluginLoadReport:
    loaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _plugin_from_object(value: Any, context: ToolContext) -> Any:
    """Normalize an entry-point or module export into a plugin object."""
    if isinstance(value, ModuleType):
        factory = getattr(value, "create_plugin", None)
        if factory is not None:
            value = factory(context)
        else:
            value = getattr(value, "plugin", None)
    if isinstance(value, type):
        try:
            value = value()
        except TypeError:
            value = value(context)
    elif callable(value) and not hasattr(value, "create_tools"):
        value = value(context)
    if value is None:
        raise PluginLoadError(
            "plugin must export create_plugin(context), plugin, or a ToolPlugin object"
        )
    if not hasattr(value, "manifest") or not callable(getattr(value, "create_tools", None)):
        raise PluginLoadError("plugin object must provide manifest and create_tools(context)")
    return value


def _validate_tools(plugin_id: str, tools: Iterable[Tool]) -> list[Tool]:
    result = list(tools)
    seen: set[str] = set()
    for tool in result:
        if not isinstance(tool, Tool):
            raise PluginLoadError(f"plugin {plugin_id} returned a non-Tool value")
        name = str(getattr(tool, "name", "")).strip()
        if not name:
            raise PluginLoadError(f"plugin {plugin_id} returned a tool without a name")
        if name in seen:
            raise PluginLoadError(f"plugin {plugin_id} returned duplicate tool: {name}")
        seen.add(name)
    return result


class PluginLoader:
    def __init__(
        self,
        registry: Any,
        context: ToolContext,
        *,
        enabled: bool = True,
        entry_points: bool = True,
        paths: Iterable[str | Path] = (),
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
        strict: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.registry = registry
        self.context = context
        self.enabled = enabled
        self.entry_points_enabled = entry_points
        self.paths = [Path(path) for path in paths]
        self.allow = {str(value) for value in allow}
        self.deny = {str(value) for value in deny}
        self.strict = strict
        self.logger = logger or logging.getLogger("braincode.plugins")

    def load(self) -> PluginLoadReport:
        report = PluginLoadReport()
        if not self.enabled:
            return report

        candidates: list[tuple[str, Any]] = []
        if self.entry_points_enabled:
            candidates.extend(self._entry_point_candidates(report))
        candidates.extend(self._path_candidates(report))

        seen_sources: set[str] = set()
        seen_plugins: set[str] = set()
        for source, candidate in candidates:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            try:
                plugin = _plugin_from_object(candidate, self.context)
                manifest = PluginManifest.from_value(plugin.manifest)
                if not self._allowed(manifest.plugin_id):
                    report.skipped.append(manifest.plugin_id)
                    continue
                if manifest.plugin_id in seen_plugins:
                    raise PluginLoadError(
                        f"plugin id '{manifest.plugin_id}' was discovered more than once"
                    )
                tools = _validate_tools(
                    manifest.plugin_id, plugin.create_tools(self.context)
                )
                self.registry.register_plugin(manifest.plugin_id, tools)
                seen_plugins.add(manifest.plugin_id)
                report.loaded.append(manifest.plugin_id)
                self.logger.info(
                    "Loaded tool plugin %s version %s (%d tool(s))",
                    manifest.plugin_id,
                    manifest.version,
                    len(tools),
                )
            except Exception as exc:
                message = f"{source}: {exc}"
                report.errors.append(message)
                self.logger.warning("Tool plugin failed to load: %s", message)
                if self.strict:
                    raise PluginLoadError(message) from exc
        return report

    def _allowed(self, plugin_id: str) -> bool:
        if plugin_id in self.deny:
            return False
        return not self.allow or plugin_id in self.allow


    def _entry_point_candidates(self, report: PluginLoadReport) -> list[tuple[str, Any]]:
        try:
            discovered = importlib.metadata.entry_points()
            if hasattr(discovered, "select"):
                entries = list(discovered.select(group=ENTRY_POINT_GROUP))
            else:
                entries = list(discovered.get(ENTRY_POINT_GROUP, []))
        except Exception as exc:
            report.errors.append(f"entry points: {exc}")
            return []

        result: list[tuple[str, Any]] = []
        for entry in sorted(entries, key=lambda item: item.name):
            source = f"entry point {entry.name}"
            try:
                result.append((source, entry.load()))
            except Exception as exc:
                message = f"{source}: {exc}"
                report.errors.append(message)
                self.logger.warning("Tool plugin failed to load: %s", message)
                if self.strict:
                    raise PluginLoadError(message) from exc
        return result

    def _path_candidates(self, report: PluginLoadReport) -> list[tuple[str, Any]]:
        result: list[tuple[str, Any]] = []
        for configured in self.paths:
            path = configured
            if not path.is_absolute():
                path = self.context.work_dir / path
            path = path.resolve()
            if path.is_file() and path.suffix == ".py":
                files = [path]
            elif path.is_dir():
                files = sorted(
                    candidate
                    for candidate in path.glob("*.py")
                    if not candidate.name.startswith("_")
                )
            else:
                self.logger.info("Tool plugin path does not exist: %s", path)
                continue
            for file_path in files:
                source = f"plugin file {file_path}"
                try:
                    module = self._load_module(file_path)
                    result.append((source, module))
                except Exception as exc:
                    message = f"{source}: {exc}"
                    report.errors.append(message)
                    self.logger.warning("Tool plugin failed to load: %s", message)
                    if self.strict:
                        raise PluginLoadError(message) from exc
        return result

    @staticmethod
    def _load_module(path: Path) -> ModuleType:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        module_name = f"braincode_external_tool_plugin_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginLoadError("could not create an import specification")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def load_plugins(
    registry: Any,
    *,
    work_dir: str | Path,
    config: Any = None,
    permission_checker: Any = None,
    plugin_config: Any = None,
    services: dict[str, Any] | None = None,
) -> PluginLoadReport:
    """Load configured plugins using a small convenience wrapper."""
    settings = plugin_config or type(
        "PluginSettings",
        (),
        {
            "enabled": True,
            "entry_points": True,
            "paths": [".braincode/plugins"],
            "allow": [],
            "deny": [],
            "strict": False,
        },
    )()
    root = Path(work_dir).resolve()
    context = ToolContext(
        work_dir=root,
        config=config,
        permission_checker=permission_checker,
        services=dict(services or {}),
    )
    loader = PluginLoader(
        registry,
        context,
        enabled=settings.enabled,
        entry_points=settings.entry_points,
        paths=settings.paths,
        allow=settings.allow,
        deny=settings.deny,
        strict=settings.strict,
    )
    return loader.load()
