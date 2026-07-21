# RecentToolCallTracker 会话级工具循环治理方案

## 1. 文档目的

本文档描述 Braincode 新增会话级 `RecentToolCallTracker` 的需求、设计边界、接入位置和验收标准，供后续 Codex 新会话理解项目现状并实施功能。

本需求解决的是 Agent 在一次任务中反复调用相同工具、得到相同结果，导致无效工具执行和上下文 token 增长的问题。它不是通用命令缓存，也不应演变成一套持久化执行状态系统。

## 2. 当前实现与问题

Braincode 当前由 `braincode/agent.py` 负责工具循环。模型每产生一个新的 `ToolCallComplete`，Agent 都会重新进入工具执行流程。系统没有根据“工具名 + 参数”判断本次调用是否与之前重复。

当前已有机制不能替代工具循环治理：

- `FileCache` 缓存 `ReadFile` 的文件正文，减少磁盘读取，但不会阻止第二次工具调用。
- `FileStateCache` 记录文件内容和 `mtime_ns`，用于 read-before-edit 和并发修改检测，不负责调用去重。
- `ConversationManager` 和 Session JSONL 保存 `tool_use`、`tool_result`，用于上下文和恢复，不会查询历史结果来阻止执行。
- `RuntimeEventBus` 保存有限的运行事件，用于通知和观察，不是工具结果缓存。
- `SQLiteJobStore` 管理后台 Job 的持久化状态、claim、lease、heartbeat 和恢复，不覆盖普通前台工具调用。
- Agent 已有最大迭代数和连续三次未知工具调用熔断，但没有对已知工具的重复调用进行检测。

例如，模型连续两次调用：

```text
Bash("pytest tests/test_agent.py")
Bash("pytest tests/test_agent.py")
```

即使代码没有变化，两次命令仍会分别执行。这个行为本身应当保留，因为测试可能依赖环境变量、数据库、网络、时间、依赖和临时文件，不能仅凭命令参数相同复用旧结果。

真正需要治理的是：同一次 Agent 任务中，模型连续多次执行相同调用并持续得到相同观察结果，却没有改变策略。

## 3. 设计原则

1. 不建设通用、持久化的工具执行状态表。
2. 不默认复用 Bash、EditFile、WriteFile 等有副作用工具的历史结果。
3. 第一阶段仍然真实执行工具，只检测重复、压缩重复结果并提醒模型。
4. Tracker 为内存态、有界、可丢失，不写入 SQLite 或 Session 文件。
5. 每个 Agent 实例拥有独立 Tracker，Subagent 之间不共享状态。
6. 重复治理以一次用户任务为强制边界，不能阻止用户在下一次提问中主动重跑测试。
7. Hook 修改工具参数后，必须使用实际执行参数计算调用指纹。
8. 三条工具执行路径必须保持一致，不能只覆盖 TUI 或前台执行。

## 4. 作用域与生命周期

`RecentToolCallTracker` 挂载在 `Agent` 实例上，但每次开始处理新的用户任务时调用 `begin_run()`：

```text
同一次用户任务内的多轮工具调用 -> 参与重复检测
下一次用户主动提问             -> 重新开始强制计数
应用重启或 Session 恢复         -> 不恢复 Tracker
不同 Agent/Subagent             -> 各自独立
```

Tracker 可以保留少量跨任务统计用于日志，但警告和熔断阈值只能基于当前 `run_id`，避免把用户明确要求的重复执行误判为模型循环。

## 5. 数据模型

建议新增文件：

```text
braincode/tools/recent_calls.py
```

核心数据结构：

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepeatPolicy(StrEnum):
    OBSERVE = "observe"
    WARN = "warn"
    GUARD = "guard"


@dataclass
class RecentToolCall:
    fingerprint: str
    tool_name: str
    arguments_json: str
    call_count: int = 0
    same_result_count: int = 0
    last_result_hash: str | None = None
    last_tool_use_id: str | None = None
    in_flight: int = 0


@dataclass(frozen=True)
class CallContext:
    fingerprint: str
    run_id: int
    call_number: int


@dataclass(frozen=True)
class RepeatDecision:
    repeated: bool
    same_result_count: int
    compact_output: str | None = None
    warning: str | None = None
    block_next: bool = False
```

Tracker 对外接口建议保持简单：

```python
class RecentToolCallTracker:
    def begin_run(self) -> None: ...

    def before_call(
        self,
        tool_name: str,
        arguments: dict,
        policy: RepeatPolicy,
    ) -> CallContext: ...

    def after_call(
        self,
        context: CallContext,
        tool_use_id: str,
        output: str,
        is_error: bool,
    ) -> RepeatDecision: ...
```

内部使用有容量限制的 `OrderedDict` 或等价 LRU 结构，默认最多保存当前任务最近 64 个不同 fingerprint。Tracker 不保存完整历史输出，只保存结果摘要、最近工具调用 ID 和必要计数。

## 6. 调用指纹

调用指纹由工具名称和规范化参数生成，不包含每次都不同的 `tool_use_id`：

```python
import hashlib
import json


arguments_json = json.dumps(
    arguments,
    sort_keys=True,
    ensure_ascii=False,
    separators=(",", ":"),
)
fingerprint = hashlib.sha256(
    f"{tool_name}\0{arguments_json}".encode("utf-8")
).hexdigest()
```

因此下面两个参数字典应生成相同指纹：

```json
{"file_path": "a.py", "offset": 0}
{"offset": 0, "file_path": "a.py"}
```

参数不能被序列化时，应采用稳定的降级表示并记录 debug 日志，不能使工具执行失败。

## 7. 重复结果判定

工具完成后，使用模型最终可见的 `output` 和 `is_error` 计算结果摘要：

```python
result_hash = sha256(
    f"{int(is_error)}\0{output}".encode("utf-8")
).hexdigest()
```

如果 fingerprint 相同但结果 hash 发生变化，`same_result_count` 应重置为 1。这表示工具调用虽然相同，但外部状态发生了变化，不属于无效重复观察。

如果 fingerprint 和结果 hash 都相同，则增加 `same_result_count`。

## 8. 第一阶段策略

第一阶段不阻止任何真实执行，只做结果压缩和循环提醒：

```text
第一次调用：
  正常执行，完整结果进入对话

第二次相同调用、结果也相同：
  仍然执行
  UI 保留完整结果
  ConversationManager 只写入简短重复说明

第三次相同调用、结果仍相同：
  仍然执行
  返回明确循环提醒，要求模型改变策略

后续继续重复：
  保持短结果
  记录 warning/runtime event
```

重复结果的对话输出示例：

```text
Repeated tool call: result unchanged from tool use `toolu_123`.
Do not repeat this call unless the underlying state has changed.
```

第三次重复时：

```text
Tool loop detected: this exact call has produced the same result 3 times.
Change strategy, inspect a different signal, or explain the blocker instead of repeating it.
```

这样可以减少重复结果进入上下文造成的 token 消耗，同时不承担错误复用命令结果的风险。

## 9. 第二阶段可选策略

第一阶段稳定并有测试数据后，可以只对明确无副作用的 `GUARD` 工具启用阻止策略。阻止依据不能只是参数相同，必须已经连续多次得到相同结果。

建议在 `Tool` 基类增加独立属性：

```python
repeat_policy: RepeatPolicy = RepeatPolicy.OBSERVE
```

不要复用 `is_concurrency_safe`，因为“可以并发执行”和“可以治理重复调用”语义不同。

建议默认策略：

| 工具 | 策略 | 说明 |
| --- | --- | --- |
| ReadFile | GUARD | 连续相同观察后可阻止继续重复 |
| Glob | GUARD | 只读目录匹配 |
| Grep | GUARD | 只读搜索 |
| ToolSearch | GUARD | 工具发现查询 |
| Bash | WARN | 依赖外部状态，不复用结果 |
| EditFile | WARN | 有文件写副作用 |
| WriteFile | WARN | 有文件写副作用 |
| MCP/插件工具 | OBSERVE | 默认不了解外部副作用 |

插件和 MCP 工具必须默认 `OBSERVE`，除非工具实现显式声明更严格策略。

## 10. Agent 接入位置

当前 `braincode/agent.py` 有三条工具执行路径：

1. `_execute_single_tool_direct()`：LLM 流式输出期间提前执行工具。
2. `_execute_tool()`：处理需要交互权限确认的前台工具。
3. `_execute_tool_noninteractive()`：用于 `run_to_completion()`、后台任务和非交互 Agent。

三条路径都必须接入 Tracker。建议在 Agent 中增加统一辅助方法，避免各路径自行拼接重复判断：

```python
def _begin_tracked_tool_call(
    self,
    tc: ToolCallComplete,
    policy: RepeatPolicy,
) -> CallContext: ...

def _finish_tracked_tool_call(
    self,
    context: CallContext,
    tc: ToolCallComplete,
    result: ToolResult,
) -> TrackedToolResult: ...
```

执行顺序：

```text
PreTool Hook 修改参数
-> 权限检查
-> Tracker.before_call
-> Pydantic 参数校验
-> tool.execute
-> PostTool Hook
-> Tracker.after_call
-> 生成 ToolResultBlock 和 UI ToolResultEvent
```

必须在 PreTool Hook 之后计算 fingerprint，因为 Hook 可能改变工具参数。结果 hash 应基于 PostTool Hook 之后模型实际可见的结果。

为了实现“UI 显示完整结果、对话只写压缩结果”，建议引入：

```python
@dataclass(frozen=True)
class TrackedToolResult:
    result: ToolResult
    conversation_output: str
    repeated: bool
    warning: str | None = None
```

- `ToolResultEvent.output` 使用原始 `result.output`。
- `ToolResultBlock.content` 使用 `conversation_output`。
- 大结果持久化和截断应作用于最终进入对话的 `conversation_output`，避免重复全文再次落盘。

## 11. 流式与并发要求

Braincode 会在收到完整 `tool_use` 后立即提交异步执行，并允许并发安全工具批量运行。因此 Tracker 必须正确处理同一批次中的相同调用：

- `before_call()` 必须在执行开始前立即增加 `in_flight`。
- `after_call()` 必须在成功、失败和异常路径中减少 `in_flight`。
- 状态更新必须是原子的，可使用 `asyncio.Lock`，或保证同步临界区内不发生 `await`。
- 第一阶段不合并并发中的两个相同调用，两者仍应真实执行。
- 两个并发结果都返回后，再根据结果 hash 更新重复计数。

不要在 `StreamingExecutor` 中直接实现业务策略。执行器只负责提交和收集任务，重复治理应属于 Agent 的工具调用生命周期。

## 12. 配置建议

第一版可以使用模块常量，避免扩大配置面：

```python
RECENT_TOOL_CALL_MAX_ENTRIES = 64
RECENT_TOOL_CALL_COMPACT_AFTER = 2
RECENT_TOOL_CALL_WARN_AFTER = 3
```

行为稳定后再考虑加入 `config.py`：

```yaml
tool_loop:
  enabled: true
  max_entries: 64
  compact_after: 2
  warn_after: 3
  guard_after: 4
```

默认配置必须保持向后兼容：关闭第二阶段 guard 时，任何已知工具都不能因为 Tracker 而被跳过。

## 13. Runtime 事件与日志

建议新增可选运行事件：

```text
tool_repeat_detected
tool_loop_warning
tool_loop_guarded
```

事件 payload 至少包含：

```json
{
  "agent_id": "...",
  "tool_name": "ReadFile",
  "fingerprint": "...",
  "call_count": 3,
  "same_result_count": 3,
  "policy": "guard"
}
```

日志和事件中不要记录完整 Bash 命令输出或敏感参数。fingerprint 可用于关联，参数只能输出经过现有脱敏策略处理的摘要。

## 14. 测试要求

建议新增 `tests/test_recent_tool_calls.py`，并在 `tests/test_agent.py` 补充端到端测试。

必须覆盖：

1. 参数字典顺序不同但 fingerprint 相同。
2. 工具名或参数不同不会误判。
3. 第二次相同结果生成压缩输出。
4. 相同调用但结果变化时，连续相同结果计数重置。
5. `begin_run()` 后强制计数清零。
6. Bash 重复调用仍然真实执行。
7. 第一阶段任何工具都不会被自动跳过。
8. 原始结果进入 UI，压缩结果进入 `ToolResultBlock`。
9. 流式执行、交互执行和 `run_to_completion()` 行为一致。
10. 同一批次并发相同调用不会破坏计数或遗留 `in_flight`。
11. 工具异常、参数校验失败、权限拒绝不会造成 Tracker 状态泄漏。
12. 超过最大条目数后按 LRU 淘汰旧记录。
13. Subagent 之间 Tracker 状态隔离。
14. 第二阶段启用后，只有 `GUARD` 工具能被阻止。

## 15. 分阶段实施顺序

### Phase 1：观察与 token 治理

1. 实现规范化 fingerprint 和有界 Tracker。
2. 接入三条工具执行路径。
3. 所有工具继续真实执行。
4. 重复相同结果时压缩写入 Conversation 的内容。
5. 达到阈值后向模型返回循环提醒。
6. 增加单元测试和 Agent 端到端测试。

### Phase 2：只读工具 guard

1. 在 `Tool` 基类增加 `repeat_policy`。
2. 为内置只读工具显式声明 `GUARD`。
3. 对插件和 MCP 工具保持 `OBSERVE` 默认值。
4. 仅在连续多次得到相同结果后阻止下一次重复执行。
5. 增加 Runtime 事件和可观测指标。

### Phase 3：基于数据调参

1. 统计重复调用数量、工具类型和压缩 token 估算。
2. 验证是否存在误报和正常轮询场景。
3. 决定是否开放配置项或按工具覆盖阈值。

## 16. 非目标

本需求不包括：

- 为 Bash 建立可复用的命令结果缓存。
- 根据 Git commit 或工作区 hash 复用测试结果。
- 把普通工具调用自动转换成持久化 Job。
- 在 Session 恢复时恢复 Tracker 状态。
- 对有副作用工具自动返回上一次执行结果。
- 替换现有 `FileCache`、`FileStateCache`、JobStore 或工具结果预算机制。

## 17. 验收标准

功能完成后应满足：

1. 同一次任务中重复调用相同工具仍会真实执行，默认行为不被破坏。
2. 连续得到相同结果时，第二次开始不再把完整重复内容写入上下文。
3. 达到阈值后模型能收到明确、简短的循环提醒。
4. 新用户任务可以正常重新执行相同命令，不受上一任务影响。
5. 三条工具执行路径和 Subagent 行为一致。
6. Tracker 内存有明确上限，不产生持久化状态和清理负担。
7. Bash、EditFile、WriteFile 不会因为参数重复而自动复用历史结果。
8. 现有 Agent、工具、权限、Hook、Session、Job 和上下文测试全部通过。

## 18. Codex 实施提示

在新会话中实施本需求时，应先阅读：

```text
braincode/agent.py
braincode/tools/base.py
braincode/tools/__init__.py
braincode/tools/file_state_cache.py
braincode/conversation.py
braincode/context/manager.py
tests/test_agent.py
```

先确认当前分支是否已经存在重复治理或工具执行重构，再按最小改动原则实施 Phase 1。不要在没有测试的情况下同时重构三条工具执行路径，也不要把本需求扩展为通用命令缓存或新的持久化数据库。
