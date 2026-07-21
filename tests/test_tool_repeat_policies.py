from __future__ import annotations

from braincode.mcp.tool_wrapper import MCPToolWrapper
from braincode.tools.base import RepeatPolicy, Tool
from braincode.tools.bash import Bash
from braincode.tools.edit_file import EditFile
from braincode.tools.glob import Glob
from braincode.tools.grep import Grep
from braincode.tools.impl.tool_search import ToolSearchTool
from braincode.tools.read_file import ReadFile
from braincode.tools.write_file import WriteFile


def test_builtin_repeat_policies_match_side_effect_risk():
    assert ReadFile.repeat_policy == RepeatPolicy.GUARD
    assert Glob.repeat_policy == RepeatPolicy.GUARD
    assert Grep.repeat_policy == RepeatPolicy.GUARD
    assert ToolSearchTool.repeat_policy == RepeatPolicy.GUARD
    assert Bash.repeat_policy == RepeatPolicy.WARN
    assert EditFile.repeat_policy == RepeatPolicy.WARN
    assert WriteFile.repeat_policy == RepeatPolicy.WARN


def test_extension_tools_default_to_observe_unless_explicitly_overridden():
    class PluginTool(Tool):
        pass

    class GuardedPluginTool(Tool):
        repeat_policy = RepeatPolicy.GUARD

    assert PluginTool.repeat_policy == RepeatPolicy.OBSERVE
    assert MCPToolWrapper.repeat_policy == RepeatPolicy.OBSERVE
    assert GuardedPluginTool.repeat_policy == RepeatPolicy.GUARD
