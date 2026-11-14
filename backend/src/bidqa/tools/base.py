"""工具基座（docs/02 D 域统一约定；D7 前半）。

约定：
- 每个工具定义 Pydantic 入参 schema（进契约测试）；schema 即 OpenAI function-calling
  parameters 与提示词工具表（C2）的同源出处。
- 空结果一律返回结构化「不可用」而非空串（empty=True 固定话术）。
- 超时/异常格式由 ToolRunner 统一产出（runner.py），工具本体只负责成功路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ValidationError

EMPTY_TEXT = "无匹配数据。可尝试更换关键词或放宽限定条件。"


def tool_result(text: str, sources: list[dict] | None = None,
                charts: list[dict] | None = None) -> dict[str, Any]:
    """成功路径的统一结果结构（B7 组装消费 sources/charts）。"""
    return {"text": text, "sources": sources or [], "charts": charts or [], "empty": False}


def empty_result(text: str = EMPTY_TEXT) -> dict[str, Any]:
    return {"text": text, "sources": [], "charts": [], "empty": True}


class BaseTool(ABC):
    """取数工具基类：name/description/args_model 三要素 + run。"""

    name: str
    description: str
    args_model: type[BaseModel]

    @abstractmethod
    def run(self, args: BaseModel) -> dict[str, Any]:
        """执行取数；返回 tool_result()/empty_result() 结构。"""

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        """坏参数在此统一暴露（D7：也返回 tool 消息而非抛出）。"""
        return self.args_model.model_validate(arguments)

    def openai_schema(self) -> dict[str, Any]:
        """OpenAI function-calling tools 参数格式（C3 用）。"""
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }


class ToolRegistry:
    """工具注册表：注册/获取/枚举；schema 与提示词表同源（C2）。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def prompt_table(self) -> str:
        """C2 工具表文本（与 openai_schemas 同源挂载）。"""
        lines = []
        for t in self._tools.values():
            params = ", ".join(t.args_model.model_json_schema().get("properties", {}))
            lines.append(f"- {t.name}({params}): {t.description}")
        return "\n".join(lines)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> BaseModel:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)
        try:
            return tool.validate(arguments)
        except ValidationError:
            raise
