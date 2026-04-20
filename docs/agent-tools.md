Agent能力支持方案调研（Tools / MCP / Skills）
结论
在当前 txuw-xiaoai 项目中，最可落地、可读性最好、扩展性也最稳的方案是把 Agent 能力拆成三层：
1. Native Tools 层：项目内用 Agent SDK 原生 Tool 注册，承载本地函数能力。
2. MCP Adapter 层：通过一个 mcp.yaml 声明外部 MCP Server，启动时自动加载、发现并拉取 tools，统一 merge 到 Tool Registry。
3. Skills 层：不把 Skills 直接等同于 Tool；而是把 Skills 设计成“可版本化的能力包 / 任务模板 / 专家配置”，用于封装 prompts、tool 组合、参数约束、执行策略和可选子 Agent。
一句话说：
Tools 负责“做事”，MCP 负责“接外部能力”，Skills 负责“把能力组织成可复用、可维护、可运营的任务单元”。
这比把 Skills 也硬塞成 Tool 更清晰，也更容易长期演进。

---
一、结合当前项目现状的判断
1.1 当前项目的 Agent 集成状态
当前 server 已经具备一个比较清晰的 Agent 接入起点：
- 依赖里已经引入 openai-agents，见 server/pyproject.toml
- 当前 Agent 入口位于：server/src/txuw_xiaoai_server/xiaoai_handlers/agent/agent.py
- 现状实现为：
这说明当前项目已经有了Agent Runtime 的最小闭环，但是还停留在：
- 只有单一 Agent
- 没有 Tool Registry
- 没有 Tool 分类与生命周期
- 没有 MCP Host 能力
- 没有 Skill 抽象
- 没有权限边界 / 风险分级 / 动态启停
因此，最合理的演进方式不是直接把 Tools / MCP / Skills 混在现有 AgentStreamService 里继续堆逻辑，而是：
以当前 AgentStreamService 为最薄入口，向外抽出一层“Capability Runtime（能力运行时）”。

---
二、推荐的总体架构
推荐在 server/src/txuw_xiaoai_server/ 下新增一个独立能力域，例如：
capabilities/
  registry/
    tool_registry.py
    tool_descriptor.py
    tool_source.py
  tools/
    builtin/
      weather.py
      speaker.py
      music.py
    wrappers/
      agent_sdk.py
  mcp/
    config_loader.py
    client_pool.py
    discovery.py
    adapter.py
    models.py
  skills/
    loader.py
    models.py
    resolver.py
    executor.py
    builtins/
      weather_query/
        skill.yaml
      smart_home/
        skill.yaml
  runtime/
    capability_runtime.py
    agent_factory.py
    toolset_builder.py
    policy.py
核心关系：
- Tool Registry：统一维护当前可用 tools
- MCP Loader：把远端/本地 MCP server 暴露的 tools 同步进 registry
- Skill Loader：读取 skills 配置，把 prompt、tool 白名单、参数模板、执行策略组织起来
- Capability Runtime：按会话 / 请求上下文动态决定本轮 agent 应该装配哪些 tools、skills、策略
- Agent Factory：最终把 Agent SDK 所需的 Agent 对象构建出来
这个拆法的优点：
- Agent SDK 只是运行时外壳，不污染业务域
- MCP 和本地 tools 被统一抽象成同一种“可调用能力”
- Skills 不和 Tool 一层概念打架，而是站在编排层
- 后续支持权限、灰度、审计、缓存、禁用某类工具都很自然

---
三、Tools 的最佳实践方案
3.1 原则
对于项目内原生能力，建议直接走 Agent SDK 原生 Tools，但不要在业务代码里零散创建 Tool；而是统一走 Tool Registry。
也就是说：
- 对外执行：Agent SDK Tool
- 对内管理：Registry + Descriptor + Policy
而不是在 Agent(...) 初始化时手写一堆 tools=[...]。
3.2 推荐抽象
建议引入统一的数据结构：
@dataclass(slots=True)
class ToolDescriptor:
    name: str
    description: str
    source: Literal["builtin", "mcp"]
    category: str
    risk_level: Literal["low", "medium", "high"]
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    agent_tool: Any | None = None
    origin: str = ""
Registry 只做三件事：
1. 注册
2. 查询/过滤
3. 按上下文组装
例如：
- 面向语音问答默认只给低风险 tools
- 涉及设备控制时追加 speaker.* / iot.*
- 涉及外部系统操作时要求 skill 显式声明或策略放行
3.3 本项目里的落地建议
优先把 tools 分成三类：
A. 内部设备类 Tools
例如：
- 播放/暂停/音量控制
- 设备状态查询
- 会话状态管理
- 主动播报 / 打断控制
这些 tools 最适合直接包在本项目 server 内。
B. 信息查询类 Tools
例如：
- 天气
- 时间
- 新闻摘要
- 日历查询
优先做成低风险 builtin tools，也可后续迁移到 MCP。
C. 外部集成类 Tools
例如：
- Home Assistant
- 飞书
- GitHub
- 搜索
这类更适合优先走 MCP，而不是直接把 SDK/API 硬写进主项目。
3.4 为什么这样更好
这样能避免两个问题：
- agent.py 变成“大杂烩构造器”
- 后续每加一个能力都要改 Agent 初始化逻辑
最终模式应当是：
agent = agent_factory.build_agent(session_context)
而不是：
agent = Agent(..., tools=[tool_a, tool_b, tool_c, ...])

---
四、MCP 的最佳实践方案
你的设想是正确的，而且建议继续强化：
通过一份 mcp.yaml 来声明 MCP Servers，服务启动时自动加载、建立连接、发现 tools，并 merge 进统一 registry。
这是当前最适合做可扩展外部能力接入的方案。
4.1 推荐的 mcp.yaml 结构
建议配置不要只存“连接参数”，还要包含：
- 启用状态
- transport 类型
- 鉴权配置
- tool 过滤规则
- 超时/重试
- 标签与风险等级
- 是否启动时预热
- 是否允许动态刷新
示例：
version: 1
servers:
  - name: github
    enabled: true
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
    include_tools: ["create_issue", "list_pull_requests", "get_file_contents"]
    exclude_tools: []
    tags: ["external", "github"]
    risk_level: medium
    startup_timeout_seconds: 15
    tool_call_timeout_seconds: 30
    auto_discover_on_boot: true

  - name: homeassistant
    enabled: true
    transport: http
    url: "https://ha.example.com/mcp"
    headers:
      Authorization: "Bearer ${HA_TOKEN}"
    include_tools: ["turn_on", "turn_off", "get_entity_state"]
    tags: ["iot", "home"]
    risk_level: high
    startup_timeout_seconds: 10
    tool_call_timeout_seconds: 20
    auto_discover_on_boot: true
4.2 启动期行为建议
启动时流程建议固定为：
1. 读取 mcp.yaml
2. 对 enabled=true 的 server 建立 client
3. 完成 capability negotiation
4. 执行 tools/list
5. 根据 include/exclude 做过滤
6. 为每个 MCP tool 包装一个统一的 adapter
7. 注册到 Tool Registry
8. 输出启动日志和健康状态
也就是说，MCP 工具不要直接散落在 Agent 内部，而是先“标准化”再进入统一注册中心。
4.3 MCP Adapter 的推荐职责
每个 MCP tool 最终应该被包装成统一对象，例如：
class McpBackedTool:
    def __init__(self, server_name: str, tool_name: str, client_pool: McpClientPool):
        ...

    async def __call__(self, **kwargs):
        ...
它要负责：
- 路由到对应 MCP server
- 调用 tools/call
- 统一异常映射
- 结果裁剪/格式规范化
- 指标埋点
- 审计日志
4.4 MCP 不建议直接 merge 的东西
这里有一个关键点：
推荐 merge：
- tools
不推荐 v1 就 merge：
- resources
- prompts
原因：
- tools 很适合统一注册
- resources 与 prompts 更像上下文层/模板层，不适合直接扁平 merge 到 tool 集合里
- 如果 v1 全都混进来，会导致抽象失焦
更好的做法是：
- v1 只接 MCP tools
- v2 再引入 MCP prompts -> Skill templates 映射
- v3 再引入 MCP resources -> context provider 映射
这样项目演进会更稳。
4.5 MCP 的工程边界建议
强烈建议给 MCP 增加以下治理能力：
- server 级别熔断
- tool 调用超时
- tool 调用并发上限
- server 健康检查
- 热重载（可选）
- 白名单/黑名单
- 工具名冲突处理
工具名冲突建议用 namespace：
- github.create_issue
- homeassistant.turn_on
- feishu.create_doc
不要直接裸露 create_issue、turn_on 这种名字，否则后面一定冲突。

---
五、Skills 的最佳落地方案（重点）
这里是最关键的结论：
Skills 最佳实践不是“另一种 Tool”，而是“面向任务场景的能力编排单元”。
这是最适合你当前项目长期演进的设计。
5.1 为什么 Skills 不应直接等于 Tool
如果把 Skill 也做成 Tool，短期看简单，长期会出现几个问题：
1. 抽象重叠：Skill 和 Tool 都在“执行动作”，边界会越来越模糊。
2. 可维护性下降：prompt、前置检查、tool 白名单、错误恢复都无处安放。
3. 运营困难：无法清晰知道“这个能力包”到底依赖哪些 tool、适合哪些意图、风险多高。
4. 测试困难：Skill 通常是多步行为，天然比单个 Tool 粒度更大。
所以建议：
- Tool = 原子能力
- Skill = 任务级编排模板
5.2 Skills 推荐定义
一个 Skill 至少应包含：
- 名称 / 描述
- 适用意图或触发条件
- system prompt / task prompt 模板
- 允许使用的 tools 白名单
- 可选禁止使用的 tools
- 参数提取规则
- 执行策略（一次调用、ReAct、多轮、handoff）
- 输出风格约束
- 风险策略
- 是否允许主动播报中间状态
建议 Skill 使用独立 yaml，例如：
name: weather_query
version: 1
summary: 查询天气并生成适合语音播报的简洁回答
trigger:
  intents: ["weather", "forecast"]
  keywords: ["天气", "温度", "下雨", "预报"]

agent:
  instructions: |
    你是一个中文语音助手。
    回答必须口语化、简洁、适合直接播报。
    优先调用天气工具获取实时信息，不要编造天气。
  output_style:
    max_sentences: 3
    plain_text_only: true

tools:
  allow:
    - weather.get_current
    - weather.get_forecast
  deny: []

execution:
  mode: react
  allow_intermediate_tts: true
  intermediate_tts_template: "我查一下天气。"

policy:
  risk_level: low
  require_confirmation: false
5.3 Skills 的执行方式
推荐执行链路：
1. 用户输入进入意图识别/路由
2. 命中某个 skill（或默认 skill）
3. Skill Resolver 产出本轮运行配置：
4. Agent Factory 基于这个配置创建 Agent
5. Agent 执行后把结果继续走当前 TTS 链路
换句话说：
Skill 不直接执行，它负责“决定这轮 Agent 怎么执行”。
5.4 推荐的 Skill 分类
在你的项目里，建议 Skills 分三层：
A. Domain Skills
面向明确任务域：
- weather_query
- smart_home_control
- feishu_assistant
- github_assistant
B. System Skills
面向系统行为：
- fallback_chat
- safe_reject
- clarification
- confirmation_required
C. Composite Skills
面向复杂编排：
- morning_briefing
- work_summary
- schedule_and_notify
v1 建议先从 Domain Skills + System Skills 开始，不要一上来做太多 Composite Skills。
5.5 Skills 最佳实践：目录式能力包
最佳落地建议不是“一个巨大的 skills.yaml”，而是：
一个 skill 一个目录，一个目录就是一个能力包。
推荐结构：
skills/
  weather_query/
    skill.yaml
    prompt.md
    examples.md
    tests.yaml
  smart_home_control/
    skill.yaml
    prompt.md
    tests.yaml
  fallback_chat/
    skill.yaml
好处：
- 便于版本管理
- 便于单独测试
- 便于后续发布/共享
- prompt、样例、策略可以拆开
- 将来如果要做“下载第三方 skills”也自然
5.6 Skills 与 MCP 的关系
最推荐的关系是：
- Tool Registry 收纳 builtin tools + MCP tools
- Skill 只声明自己允许使用哪些 tools
- Skill 不直接关心 tool 来源
例如：
- github_assistant 可以允许 github.*
- smart_home_control 可以允许 homeassistant.*
- 将来 weather_query 从 builtin weather 切到 mcp weather，对 Skill 来说几乎无感
这就是可扩展性的关键。

---
六、当前项目的具体落地建议
6.1 不建议直接把所有逻辑写进 AgentStreamService
当前 AgentStreamService 很薄，这是好事，建议继续保持它薄。
它适合负责：
- 调 Agent SDK
- 拿流式输出
- 不关心 tools/mcp/skills 的加载细节
建议把它改造成：
class AgentRuntimeService:
    async def stream_text(self, prompt: str, session_context: SessionContext) -> AsyncIterator[str]:
        runtime = self._capability_runtime.resolve(session_context, prompt)
        agent = self._agent_factory.build(runtime)
        ...
也就是：
- CapabilityRuntime 决定“这轮有哪些能力可用”
- AgentFactory 决定“如何构造 Agent SDK Agent”
- AgentRuntimeService 只管执行
6.2 推荐新增的关键对象
SessionContext
建议引入一个显式上下文：
@dataclass(slots=True)
class SessionContext:
    connection_id: str
    dialog_id: str
    locale: str = "zh-CN"
    channel: str = "xiaoai"
    user_id: str | None = None
    device_id: str | None = None
    scene: str = "voice_assistant"
CapabilityResolution
@dataclass(slots=True)
class CapabilityResolution:
    instructions: str
    tools: list[Any]
    skill_name: str
    output_mode: str
    allow_intermediate_tts: bool
这样后续扩展不会污染核心链路。
6.3 推荐演进顺序
Phase 1：Tool Registry
先把 builtin tools 抽象好，哪怕一开始只有 2~3 个。
Phase 2：MCP Host 接入
支持 mcp.yaml + 启动时发现 tools + merge registry。
Phase 3：Skill Runtime
支持 skill 目录加载、match、resolve。
Phase 4：Policy / Guardrails
增加高风险 tool 二次确认、只读/写操作分级。
Phase 5：Tracing / Eval
记录 skill 命中率、tool 调用链路、失败率、语音口播质量。
这个顺序是最稳的，不建议反过来先做大而全的 skills 系统。

---
七、推荐的配置设计
7.1 总配置入口
建议不要把所有配置都塞进 .env，而是分层：
- .env：密钥、URL、环境变量覆盖
- config/tools.yaml：builtin tools 启停配置
- config/mcp.yaml：MCP server 配置
- skills/**/skill.yaml：skill 定义
这样职责清晰。
7.2 统一配置模型
建议把 config 读取统一纳入 config.py 之外的新模块，例如：
config/
  app_settings.py
  tool_settings.py
  mcp_settings.py
  skill_settings.py
避免未来 config.py 一路膨胀成几百行。

---
八、可读性与扩展性的关键设计原则
这是我认为最重要的几条：
1. 不让 Agent 知道所有细节
Agent 只拿最终 instructions + tools。
2. 不让 Skill 直接实现底层调用
Skill 只负责编排与约束。
3. 不让 MCP 直接侵入业务层
MCP 先标准化进 registry。
4. 所有能力都带元数据
至少包含：来源、风险、分类、启用状态、命名空间。
5. 命名空间优先
所有 tool 都强制 namespace，避免冲突。
6. 语音场景优先考虑中间播报策略
对于慢工具调用，Skill 要能声明：
- 是否允许插入“我查一下”
- 是否需要先确认
- 超时后如何口播降级
这对你的项目尤其重要，因为这是 voice-first，不是 chat-first。

---
九、给你的最终推荐方案（最佳实践版）
如果只给一套“现在就能落地”的最佳实践，我建议如下：
方案名：Capability Runtime + Registry + Skill Pack
9.1 核心原则
- Tools：统一注册到 Tool Registry，底层用 Agent SDK 原生 Tools 封装
- MCP：通过 mcp.yaml 配置 server，启动自动发现 tools 并 merge 到 registry
- Skills：以目录式 skill pack 落地，作为“任务编排配置”，而不是另一种 tool
9.2 v1 必做
1. 新增 ToolRegistry
2. 新增 mcp.yaml 加载与 MCP tool discovery
3. 新增 skills/ 目录式加载
4. 新增 SkillResolver
5. 新增 AgentFactory
6. AgentStreamService 升级为按 skill/runtime 构建 Agent
9.3 v1 暂不做
- 不做 MCP resources merge
- 不做 MCP prompts merge
- 不做复杂多 Agent handoff
- 不做远程 skill marketplace
- 不做自动学习型 skill 生成
9.4 v1 成果
完成后你会得到：
- 一个可持续演进的能力体系
- 一个对内清晰、对外可配置的能力接入层
- 一个适合语音 Agent 的技能编排模型
- 一个后续可以继续接 HomeAssistant / GitHub / Feishu / 搜索 / Calendar 的稳定底座

---
十、建议的目录落地版本（可直接实施）
server/src/txuw_xiaoai_server/
  capabilities/
    registry/
      tool_descriptor.py
      tool_registry.py
    mcp/
      models.py
      config_loader.py
      client_pool.py
      discovery.py
      adapter.py
    skills/
      models.py
      loader.py
      resolver.py
    runtime/
      session_context.py
      capability_runtime.py
      agent_factory.py
  config/
    mcp.yaml
  skills/
    fallback_chat/
      skill.yaml
    weather_query/
      skill.yaml
    smart_home_control/
      skill.yaml

---
十一、实施建议（两周版本）
第 1 周
- 抽 Tool Registry
- 接 2~3 个 builtin tools
- 改造 AgentFactory
- 跑通一个 weather_query skill
第 2 周
- 接 mcp.yaml
- 跑通一个 MCP server（建议先 GitHub 或一个 mock server）
- Skill 支持 allow/deny tools
- 增加 tool risk level 与日志审计
这样两周内可以拿到一个结构非常清晰的 v1。

---
十二、我的最终判断
最佳实践结论如下：
1. Tools：使用 Agent SDK 原生 Tools，但必须经由统一 Tool Registry 管理。
2. MCP：使用 mcp.yaml 配置 MCP servers，启动时自动 discover tools 并 merge 到 registry；v1 只收敛 tools，不要急着把 resources/prompts 也混进来。
3. Skills：采用“目录式 Skill Pack”，把 Skills 定义为任务编排单元，而不是 Tool；Skill 负责 prompt、tool 白名单、执行策略、输出风格和中间播报策略。
4. 架构落点：在当前项目中新增一层 Capability Runtime，不要把 tools/mcp/skills 的加载逻辑继续堆进 AgentStreamService。
这是我认为在当前项目里，兼顾可读性、可扩展性、可测试性以及语音场景适配度 的最优方案。

---
附：参考判断依据
A. OpenAI Agents SDK
公开文档强调其核心抽象是：
- Agents
- Tools
- Handoffs / Agents-as-tools
- Guardrails
- MCP server tool calling
这说明 Tools 与 MCP 在运行时层面可以统一，但并不等于业务设计上应该把 Skills 也压扁成 Tool。
B. MCP 官方架构
MCP 官方定义了 server primitives：
- tools
- resources
- prompts
其中 tools 最适合作为你当前 v1 的统一能力入口；resources/prompts 更适合后续映射到上下文与模板层。

---
对当前文档的一句行动建议
如果要立刻开工，我建议第一张任务单就写成：
在 server 侧引入 Capability Runtime，先完成 Tool Registry + weather_query skill + mcp.yaml discovery skeleton。
这条路径最短，也最不容易返工。