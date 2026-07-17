"""Minimal local Braincode tool plugin.

Copy this file to ``.braincode/plugins/hello.py`` and add ``hello-plugin`` to
``plugins.allow`` in config.yaml to make it available to the Agent.
"""

from pydantic import BaseModel

from braincode.tools.base import Tool, ToolResult


class HelloParams(BaseModel):
    name: str = "Braincode"


class HelloTool(Tool):
    name = "Hello"
    description = "Return a greeting for a name."
    params_model = HelloParams
    category = "read"
    should_defer = True

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, HelloParams)
        return ToolResult(output=f"Hello, {params.name}!")


class HelloPlugin:
    manifest = {
        "id": "hello-plugin",
        "version": "1.0.0",
        "api_version": "1",
        "namespace": "hello",
    }

    def create_tools(self, context):
        return [HelloTool()]


def create_plugin(context):
    return HelloPlugin()

