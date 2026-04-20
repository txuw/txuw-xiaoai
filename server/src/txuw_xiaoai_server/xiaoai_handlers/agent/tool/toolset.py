from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from agents import RunContextWrapper
from agents.mcp.server import MCPServer
from agents.tool import FunctionTool, Tool, function_tool

from ..runtime import AgentRunContext, DEFAULT_SKILL_PROFILE, REGION_TOOL_NAME, SkillProfile


_OPENAI_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(slots=True, frozen=True)
class AgentToolsetSpec:
    """描述某一轮 Agent 可见的最小工具集合。"""

    tools: list[Tool] = field(default_factory=list)
    mcp_servers: list[MCPServer] = field(default_factory=list)
    extra_instructions: str = ""


class AgentToolsetFactory:
    """集中装配当前项目内极少量的 Agent tools。"""

    def __init__(
        self,
        *,
        amap_web_service_key: str,
        region_tool_enabled: bool,
        region_tool_timeout_seconds: float,
    ) -> None:
        self._amap_web_service_key = amap_web_service_key
        self._region_tool_enabled = region_tool_enabled
        self._region_tool_timeout_seconds = region_tool_timeout_seconds
        self._region_tool = (
            _build_region_tool(timeout_seconds=region_tool_timeout_seconds)
            if region_tool_enabled and bool(amap_web_service_key)
            else None
        )

    def build_toolset(
        self,
        _run_context: AgentRunContext,
        *,
        skill_profile: SkillProfile | None = None,
    ) -> AgentToolsetSpec:
        profile = skill_profile or DEFAULT_SKILL_PROFILE

        # v1 当前只有一个 builtin tool，没有必要过早引入全局 registry。
        # 直接在工厂里集中装配，能把“当前 Agent 看得到什么能力”表达得更直白。
        candidate_tools: list[Tool] = []
        if self._region_tool is not None:
            candidate_tools.append(self._region_tool)

        tools = [
            tool
            for tool in candidate_tools
            if not isinstance(tool, FunctionTool) or tool.name in profile.allowed_tool_names
        ]
        for tool in tools:
            if isinstance(tool, FunctionTool):
                _ensure_openai_compatible_tool_name(tool.name)

        instructions: list[str] = []
        if any(isinstance(tool, FunctionTool) and tool.name == REGION_TOOL_NAME for tool in tools):
            instructions.append(
                "当用户询问当前地区、当前城市、当前地理归属，或询问天气但没有提供城市时，"
                "优先调用地区工具获取高德返回的省、市、adcode 和 rectangle，"
                "不要编造城市或更细粒度的地址。"
            )
        if profile.extra_instructions.strip():
            instructions.append(profile.extra_instructions.strip())

        return AgentToolsetSpec(
            tools=tools,
            mcp_servers=[],
            extra_instructions="\n".join(instructions).strip(),
        )


def _build_region_tool(timeout_seconds: float) -> FunctionTool:
    @function_tool(
        name_override=_ensure_openai_compatible_tool_name(REGION_TOOL_NAME),
        description_override="获取当前地区，可选显式指定国内 IPv4；为空时默认使用当前请求出口 IP。",
        strict_mode=True,
        timeout=timeout_seconds,
        timeout_behavior="error_as_result",
        failure_error_function=_format_region_tool_error,
        timeout_error_function=_format_region_tool_timeout_error,
    )
    async def get_current_region(
        ctx: RunContextWrapper[AgentRunContext],
        ip: str | None = None,
    ) -> dict[str, Any]:
        """获取当前地区。"""

        # 先口播进度提示，再进入查询主逻辑，这样语音场景下用户能立刻感知到工具已开始工作。
        await ctx.context.speak_progress("正在进行地区获取。")

        if ctx.context.region_service is None:
            raise RuntimeError("地区服务未初始化。")

        normalized_ip = _normalize_ip(ip)
        result = await ctx.context.region_service.lookup(normalized_ip)
        return result.model_dump(mode="json")

    get_current_region.params_json_schema = {
        "type": "object",
        "properties": {
            "ip": {
                "type": "string",
                "description": "显式指定要查询的国内 IPv4 地址；为空时使用服务端请求出口 IP。",
            }
        },
        "additionalProperties": False,
    }
    return get_current_region


def _normalize_ip(ip: str | None) -> str | None:
    if ip is None:
        return None

    normalized = ip.strip()
    if not normalized:
        return None

    parsed = ipaddress.ip_address(normalized)
    if parsed.version != 4:
        raise ValueError("高德 IP 定位仅支持国内 IPv4，不支持 IPv6。")
    return str(parsed)


def _ensure_openai_compatible_tool_name(name: str) -> str:
    if _OPENAI_TOOL_NAME_PATTERN.fullmatch(name):
        return name
    raise ValueError(
        "Agent tool 名称不兼容当前 OpenAI/LiteLLM chat.completions 接口："
        f"{name!r}。"
        "工具名只允许字母、数字、下划线和连字符。"
    )


def _format_region_tool_error(
    _ctx: RunContextWrapper[Any],
    error: Exception,
) -> str:
    return (
        "地区获取失败："
        f"{error}。"
        "请直接向用户说明当前暂时无法确定地区。"
        "高德 IP 定位仅支持国内 IPv4，不要编造具体城市或更精确地址。"
    )


def _format_region_tool_timeout_error(
    _ctx: RunContextWrapper[Any],
    _error: Exception,
) -> str:
    return "地区获取超时。请直接向用户说明当前暂时无法确定地区，不要编造具体城市或更精确地址。"
