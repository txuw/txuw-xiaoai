from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .tool import RegionLookupService


# OpenAI 兼容的 chat.completions tool name 只接受字母、数字、下划线和连字符，
# 因此这里不能继续使用带点号的 capability 风格命名。
REGION_TOOL_NAME: Final[str] = "region_get_current_region"


@dataclass(slots=True, frozen=True)
class SkillProfile:
    """描述一类 Agent 任务的轻量技能配置。"""

    name: str
    extra_instructions: str = ""
    allowed_tool_names: frozenset[str] = frozenset()
    allow_intermediate_tts: bool = False
    intermediate_tts_text: str = ""


DEFAULT_SKILL_PROFILE: Final[SkillProfile] = SkillProfile(
    name="default",
    allowed_tool_names=frozenset({REGION_TOOL_NAME}),
    allow_intermediate_tts=True,
    intermediate_tts_text="正在进行地区获取。",
)


@dataclass(slots=True)
class AgentRunContext:
    """承载单轮 Agent 运行时可复用的上下文能力。"""

    connection_id: str
    dialog_id: str
    instruction_name: str
    source: str
    locale: str
    speak_progress: Callable[[str], Awaitable[None]]
    announce_once_keys: set[str] = field(default_factory=set)
    region_service: RegionLookupService | None = None
