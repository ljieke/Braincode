# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from braincode.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from braincode.cache import FileCache


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._tool_owners: dict[str, str] = {}
        self._plugin_tools: dict[str, set[str]] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool, *, owner: str = "builtin") -> None:
        if not isinstance(tool, Tool):
            raise TypeError("tool must be an instance of Tool")
        existing_owner = self._tool_owners.get(tool.name)
        if existing_owner is not None and existing_owner != owner:
            raise ValueError(
                f"Tool '{tool.name}' is already registered by {existing_owner}"
            )
        self._tools[tool.name] = tool
        self._tool_owners[tool.name] = owner
        if owner != "builtin":
            self._plugin_tools.setdefault(owner, set()).add(tool.name)

    def register_from(self, source: "ToolRegistry", tool: Tool) -> None:
        """Copy a tool while preserving its source owner metadata."""
        self.register(tool, owner=source.get_tool_owner(tool.name) or "builtin")

    def register_plugin(self, plugin_id: str, tools: list[Tool]) -> None:
        """Atomically register all tools belonging to one plugin."""
        if not plugin_id.strip():
            raise ValueError("plugin_id must not be empty")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError(f"Plugin '{plugin_id}' contains duplicate tool names")
        for name in names:
            existing_owner = self._tool_owners.get(name)
            if existing_owner is not None:
                raise ValueError(
                    f"Tool '{name}' is already registered by {existing_owner}"
                )
        for tool in tools:
            self.register(tool, owner=plugin_id)

    def unregister_plugin(self, plugin_id: str) -> None:
        for name in self._plugin_tools.pop(plugin_id, set()):
            self._tools.pop(name, None)
            self._tool_owners.pop(name, None)
            self._disabled.discard(name)
            self._discovered.discard(name)

    def get_tool_owner(self, name: str) -> str | None:
        return self._tool_owners.get(name)

    def get_plugin_tools(self, plugin_id: str) -> list[Tool]:
        return [
            self._tools[name]
            for name in sorted(self._plugin_tools.get(plugin_id, set()))
            if name in self._tools
        ]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()


    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered


    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def search_deferred(
        self, query: str, max_results: int, protocol: str | None = None
    ) -> list[ToolDefinition]:
        # ``protocol`` is retained for callers written against the old API.
        # Definitions are now always provider-neutral.
        del protocol
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[ToolDefinition] = []
        for _, _name, tool in scored[:max_results]:
            results.append(tool.get_schema())
        return results

    def find_deferred_by_names(
        self, names: list[str], protocol: str | None = None
    ) -> list[ToolDefinition]:
        # Backward-compatible argument; serialization belongs to LLM clients.
        del protocol
        results: list[ToolDefinition] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            results.append(tool.get_schema())
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


    def get_all_definitions(self) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            definitions.append(tool.get_schema())
        return definitions

    def get_all_schemas(
        self, protocol: str | None = None
    ) -> list[ToolDefinition]:
        """Return canonical definitions regardless of the requested protocol.

        ``protocol`` remains accepted so external tools do not break during the
        migration. Provider-specific wire schemas are built by the active
        ``LLMClient`` immediately before each request.
        """
        del protocol
        return self.get_all_definitions()


def create_default_registry(file_cache: FileCache | None = None, file_history: Any = None) -> ToolRegistry:
    from braincode.tools.bash import Bash
    from braincode.tools.edit_file import EditFile
    from braincode.tools.file_state_cache import FileStateCache
    from braincode.tools.glob import Glob
    from braincode.tools.grep import Grep
    from braincode.tools.read_file import ReadFile
    from braincode.tools.write_file import WriteFile

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(ReadFile(file_cache=file_cache, file_state_cache=file_state_cache))
    registry.register(WriteFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(EditFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())
    return registry
