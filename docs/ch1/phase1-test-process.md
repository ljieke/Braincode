# Phase 1 测试流程：RecentToolCallTracker

本文档用于验证 `RecentToolCallTracker` Phase 1（观察、重复结果压缩、循环提醒）。
测试目标是让另一个 agent 可以用确定性的虚拟工具和虚拟模型复现全部行为，同时不修改、
不污染项目工作树。

## 1. 测试范围和完成定义

Phase 1 只观察重复调用，不阻止工具执行，也不复用有副作用工具的历史结果。

一次调用是否属于“同一调用”由以下两项共同决定：

```text
fingerprint = 工具名 + PreTool Hook 之后的规范化参数
result_hash  = PostTool Hook 之后的 output + is_error
```

同一个 fingerprint 连续得到相同 result_hash 时，预期行为如下：

| 次数 | 工具是否真实执行 | UI `ToolResultEvent.output` | Conversation 中的工具结果 |
| --- | --- | --- | --- |
| 第 1 次 | 是 | 完整原始结果 | 完整结果 |
| 第 2 次 | 是 | 完整原始结果 | `Repeated tool call` 压缩说明 |
| 第 3 次及以后 | 是 | 完整原始结果 | `Tool loop detected` 循环提醒 |

本流程完成的最低条件：

1. 下方 Phase 1 矩阵的每一项都有“通过”或带证据的“已知限制”记录。
2. 任何一次重复调用都没有被 Phase 1 自动跳过；虚拟工具的执行计数与调用次数一致。
3. `in_flight` 在成功、失败、异常和取消后都回到 `0`。
4. 首次结果完整进入 Conversation；重复调用的完整原始结果保留在 UI，Conversation 使用压缩结果，
   两条输出不发生串线。
5. 运行前后的项目工作树状态一致，临时副本和临时数据已删除。

## 2. 无污染测试协议

### 2.1 强制隔离规则

- 不在 `E:\py_project\Braincode` 根目录直接运行会生成缓存或会话文件的测试。
- 不调用真实 Shell、网络、数据库、文件写工具或真实 LLM；全部使用 fake tool、fake client、
  临时目录和固定字符串结果。
- 不修改源码、配置、Session JSONL、SQLite、项目样例文件或用户 home。
- 不依赖当前时间、随机数、网络返回或机器上的真实文件内容。
- 测试证据只保存到临时实验目录；需要提交的只有测试报告，不提交运行产物。

### 2.2 Windows 临时实验目录

在 PowerShell 中执行以下流程。`$repo` 是只读源目录，`$lab` 是本次测试的临时副本。
每次测试使用新的 GUID，不能复用旧实验目录。脚本也会恢复当前 PowerShell 进程的临时环境变量，
避免测试结束后污染终端环境。

```powershell
$repo = 'E:\py_project\Braincode'
$id = [guid]::NewGuid().ToString('N')
$lab = Join-Path ([System.IO.Path]::GetTempPath()) "braincode-phase1-$id"
New-Item -ItemType Directory -Path $lab -Force | Out-Null

$envNames = @(
    'PYTHONDONTWRITEBYTECODE', 'PYTHONUTF8', 'HOME', 'USERPROFILE',
    'TEMP', 'TMP', 'PYTEST_DEBUG_TEMPROOT'
)
$envBackup = @{}
foreach ($name in $envNames) {
    $envBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

# 复制当前工作树，保留未提交的实现和测试；排除 Git、缓存和 Python 字节码。
robocopy $repo $lab /E /XD .git .braincode __pycache__ .pytest_cache .mypy_cache .ruff_cache .venv /XF *.pyc *.pyo
if ($LASTEXITCODE -gt 7) { throw "临时副本复制失败，robocopy=$LASTEXITCODE" }

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:HOME = Join-Path $lab 'home'
$env:USERPROFILE = $env:HOME
$env:TEMP = Join-Path $lab 'tmp'
$env:TMP = $env:TEMP
$env:PYTEST_DEBUG_TEMPROOT = Join-Path $lab 'pytest-temp'
New-Item -ItemType Directory -Path $env:HOME, $env:TEMP, $env:PYTEST_DEBUG_TEMPROOT -Force | Out-Null

Push-Location $lab
$pytestExit = 1
try {
    # 这是现有 Phase 1 测试的基线冒烟检查，不代表第 4 节完整矩阵已通过。
    # 关闭 pytest cacheprovider，所有 basetemp 都留在临时副本内。
    python -m pytest -p no:cacheprovider --basetemp "$lab\pytest-basetemp" `
        tests/test_recent_tool_calls.py tests/test_recent_tool_calls_agent.py
    $pytestExit = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $lab -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($name in $envNames) {
        $value = $envBackup[$name]
        if ($null -eq $value) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$name" $value
        }
    }
}
if ($pytestExit -ne 0) {
    throw "Phase 1 pytest 失败，exit=$pytestExit"
}
```

### 2.3 委派模式与清理

上面是“一键基线模式”：它运行现有两个测试文件，然后无论成功或失败都清理 `$lab`。
要把临时副本交给其他 agent，使用下面的“委派模式”，只执行上面脚本中第 52-77 行的准备部分，
不要执行第 79-103 行；保留当前 PowerShell 窗口，使 `$lab`、`$envBackup` 和隔离环境变量仍然有效。

委派 agent 完成矩阵并检查报告后，在同一个 PowerShell 窗口执行清理：

```powershell
if (-not $lab -or -not (Test-Path -LiteralPath $lab)) {
    throw '找不到本次实验的临时目录，拒绝执行清理。'
}
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$labFull = [System.IO.Path]::GetFullPath($lab)
if (-not $labFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "临时目录不在系统 temp 下，拒绝删除：$labFull"
}
Remove-Item -LiteralPath $labFull -Recurse -Force
foreach ($name in $envNames) {
    $value = $envBackup[$name]
    if ($null -eq $value) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    else {
        Set-Item "Env:$name" $value
    }
}
```

注意：删除 `$lab` 前必须确认它是本次命令生成的、位于系统临时目录下的 GUID 子目录，
不能把删除命令替换成项目根目录或用户目录。若测试中途异常，先停止 Python 进程，再只删除
该 `$lab`。

### 2.4 原项目无污染证据

在创建临时副本前记录源目录状态；测试结束后再次比较。由于工作树可能已有用户改动，
不能用“目录必须干净”作为条件，只能比较前后是否相同。

```powershell
$before = (git -C $repo status --porcelain=v1) -join "`n"
# 执行 2.2 的临时副本测试
$after = (git -C $repo status --porcelain=v1) -join "`n"
if ($before -cne $after) {
    throw '源项目 git status 发生变化，测试失败；请保留差异用于排查，不要自动回滚用户改动。'
}
```

最终还要确认：

- `Test-Path $lab` 为 `False`；
- 源项目没有新增 `.braincode`、`__pycache__`、`.pytest_cache`、Session、SQLite 或报告临时文件；
- 没有把 fake 数据写入项目中的真实文件。

### 2.5 交给其他 agent 的权限边界

给执行 agent 的唯一可写目录是 `$lab`。允许它在 `$lab\tests` 创建临时矩阵测试文件，
在 `$lab` 写测试报告和日志；禁止它编辑源目录中的 Python、配置和文档。推荐约定如下：

```text
$lab\tests\phase1_matrix_virtual_test.py  # agent 临时创建，测试结束随 $lab 删除
$lab\phase1-report.md                     # agent 临时生成，确认后再人工复制最终摘要
```

执行 agent 必须使用第 3 节 fake tool/fake client，不得把真实 Bash、ReadFile、EditFile、网络或
LLM 接入矩阵。若 agent 认为必须改源码才能继续，应先报告阻塞项，不要直接修改 `$repo`。

## 3. 虚拟测试夹具

所有 agent 使用同一套确定性夹具，避免每个 agent 自己改变测试含义。

### 3.1 `CountingTool`

```python
class Params(BaseModel):
    value: str


class CountingTool(Tool):
    name = "Count"
    params_model = Params
    is_concurrency_safe = True

    def __init__(self, outputs=None, *, delay=0):
        self.calls = 0
        self.outputs = list(outputs or ["result:x"])
        self.delay = delay

    async def execute(self, params):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        output = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        return ToolResult(output=output)
```

规则：`calls` 是唯一的真实执行证据；不能通过 Tracker 的结果替代它。需要模拟错误时返回
`ToolResult(output="error", is_error=True)`，需要模拟异常时直接 `raise RuntimeError("boom")`。

### 3.2 `SequenceClient`

Mock LLM 每次 `stream()` 返回预先指定的事件，不访问网络。基础序列为：

```text
响应 1: ToolCallComplete("one",   "Count", {"value": "x"}) + StreamEnd("end_turn")
响应 2: ToolCallComplete("two",   "Count", {"value": "x"}) + StreamEnd("end_turn")
响应 3: ToolCallComplete("three", "Count", {"value": "x"}) + StreamEnd("end_turn")
响应 4: StreamEnd("end_turn")
```

每次 `tool_id` 必须不同，证明 fingerprint 不包含 `tool_use_id`。

### 3.3 Hook、权限和并发夹具

- `PreHook`: 把 `{"path": "a.py"}` 改成 `{"path": "fixture.py"}`，用于验证最终参数才参与 fingerprint。
- `PostHook`: 把工具原始输出统一改成 `"post:same"`，用于验证最终可见结果才参与 result hash。
- `ValidationTool`: 参数模型拒绝 `value="invalid"`。
- `PermissionTool`: PermissionChecker 返回 deny 或 ask；工具本身有 `calls` 计数，预期不执行。
- `GateTool`: 在两个并发调用之间用 `asyncio.Event` 放行，确保两个 `before_call()` 同时处于
  `in_flight`。
- `Subagent`: 创建两个独立 `Agent`，各自注册同名、同参数的 fake tool；不共享 Tracker。

## 4. Phase 1 测试矩阵

每一项都要记录：输入序列、fake 工具执行次数、`RepeatDecision` 或对话内容、runtime 事件、
`len(tracker)`、`tracker.in_flight`、最终结论。

### A. Tracker 纯单元测试

| ID | 场景和虚拟数据 | 操作 | 预期断言 |
| --- | --- | --- | --- |
| A1 | 参数 `{"file_path":"a.py","offset":0}` 与同字段倒序字典 | 计算规范化 JSON 和 fingerprint | 两个规范化 JSON 相同，`ReadFile` fingerprint 相同 |
| A2 | `ReadFile(a.py)`、`Grep(a.py)`、`ReadFile(b.py)` | 各完成一次，输出均为 `same` | 三者互不误判，均为首次调用 |
| A3 | `Bash({"command":"pytest"})` 连续三次，输出 `same/False` | 分别调用 `before_call`、`after_call` | 第 1 次完整；第 2 次 `repeated=True` 且有压缩文本；第 3 次有 warning；`block_next=False` |
| A4 | 同 fingerprint 的结果依次为 `R1/False`、`R2/False`、`R1/False`、`R1/False`、`R1/True` | 连续完成五次 | 计数依次为 `1,1,1,2,1`；旧结果不能复活旧计数；`is_error` 参与 hash |
| A5 | 先在 run 1 完成 A，再 `begin_run()`，run 2 再完成 A | 比较两次状态 | run 2 首次调用，不继承 run 1 计数；`run_id` 变化，计数清零 |
| A6 | `max_entries=2`，按 A、B、触碰 A、C 插入 | 查看 LRU | B 被淘汰，A 保留，最终条目数不超过 2 |
| A7 | `max_entries=1`，A/B 同时 `before_call`，再分别完成 | 观察活动条目和收尾 | 活动条目不被淘汰；完成后 `len(tracker) <= 1`、`in_flight == 0` |
| A8 | 参数含 bytes、PathLike、Enum、自定义对象、循环引用 | 计算规范化参数 | 不抛出序列化异常；相同值产生稳定表示；工具仍可继续执行 |
| A9 | 两个相同 fingerprint 同时开始，输出都为 `same` | `asyncio.gather()` 完成两个 `after_call` | 两次都真实完成，`call_count == 2`、`same_result_count == 2`，无负数或残留 `in_flight` |
| A10 | 开始调用后不完成，改走 `abandon_call()` | 放弃调用 | `in_flight` 归零；后续相同调用可以正常开始和完成 |

### B. Agent 端到端和三条执行路径

| ID | 场景和虚拟数据 | 操作 | 预期断言 |
| --- | --- | --- | --- |
| B1 | `SequenceClient` 的 A、A、A 序列；`CountingTool` 输出 `result:x` | `async for event in agent.run(conversation)` | 工具 `calls == 3`；三次 `ToolResultEvent.output` 都是完整 `result:x`；Conversation 依次为完整、压缩、warning |
| B2 | 同上，收集 runtime sink | 检查事件类型和 payload | 只出现 `tool_repeat_detected`、`tool_loop_warning`；包含 agent、工具名、fingerprint、call count、same-result count；不含完整敏感输出 |
| B3 | 直接路径 `_execute_single_tool_direct()` | 连续执行同一 fake tool 三次 | 与 B1 相同；未知工具和 disabled 工具保持原有错误行为，不产生错误 tracker 记录 |
| B4 | 交互权限路径 `_execute_tool()` | 用 permission ask/deny 和允许三种决定执行同一调用 | 允许时重复策略与 B1 一致；deny/ask 时工具不执行，PermissionRequest/错误结果按原约定返回，且无 `in_flight` 泄漏 |
| B5 | 非交互路径 `_execute_tool_noninteractive()` | 连续执行同一调用三次 | 与流式路径相同；工具仍执行三次，返回值本身不被替换成历史结果 |
| B6 | `run_to_completion()` 使用确定性 `SequenceClient` | 完成 A、A、A 后结束 | `begin_run()` 在任务开始生效；同一任务内按 B1 压缩；下一次 `run_to_completion()` 首次调用不重复 |
| B7 | `Bash`、`EditFile`、`WriteFile` 名称的无副作用 fake tool | 每种工具各重复三次 | Phase 1 一律真实执行；不能因名称或参数重复自动跳过或返回旧结果 |
| B8 | UI 与 Conversation 分流 | 检查 `ToolResultEvent`、`ToolResultBlock` 和 `conversation.history` | UI 看到原始完整结果；Conversation 只在第二次开始看到压缩/警告；两者的 `is_error` 一致 |
| B9 | PreHook 将不同原始参数改成同一最终参数 | 原始参数 A、B 各执行两次 | Hook 后相同参数使用相同 fingerprint；第二次最终相同结果进入压缩路径 |
| B10 | PostHook 将不同原始输出改成同一最终输出；再改成不同输出 | 连续执行并比较计数 | hash 使用 PostHook 后 output；最终输出变化时连续计数重置 |

### C. 错误、取消、边界和隔离

| ID | 场景和虚拟数据 | 操作 | 预期断言 |
| --- | --- | --- | --- |
| C1 | 参数校验失败 | 连续两次传 `value="invalid"` | 返回参数校验错误；不抛出未处理异常；执行结束 `in_flight == 0`；是否重复只按实际错误结果计数 |
| C2 | 工具 `raise RuntimeError("boom")` | 执行一次并收集 Agent 转换后的错误结果，再执行正常调用 | 不向 Agent 循环泄漏未处理异常；失败路径清理 Tracker；正常调用可继续；无活动调用泄漏 |
| C3 | PreTool Hook 抛错或返回 hook error | 执行一次 | 工具不执行；不产生 phantom call；`in_flight == 0` |
| C4 | 权限 deny 和 ask | 分别执行一次 | 工具 `calls == 0`；错误/请求结果符合权限模块原约定；Tracker 没有活动调用泄漏 |
| C5 | 执行任务被取消 | 在 `GateTool` 等待时取消 asyncio task | `abandon_call()` 或等价清理被触发；`in_flight == 0`；后续调用仍可执行 |
| C6 | 同一批次两个相同调用 | 同时提交两个 `ToolCallComplete` | 两个都真实执行；结果收齐后计数正确；不因并发顺序丢失或重复扣减 |
| C7 | 两个独立 Agent/Subagent | 各自执行同名同参数一次，再各自执行第二次 | 两个 Agent 的第一次都不是重复；第二次只在各自 Tracker 内判定；互不影响 |
| C8 | Session/应用边界 | 新建 Agent 或调用 `begin_run()` 后重复 A | 新任务首次执行完整；不从 Session JSONL、SQLite 或旧 Agent 恢复 Tracker |
| C9 | 容量满且所有记录 active | `max_entries=2`，同时开始 A、B、C | 允许短暂活动溢出但不得淘汰 active 条目；所有调用收尾后裁剪到上限；记录该行为作为当前实现约定 |
| C10 | 长输出和敏感参数 | 输出固定长字符串及伪敏感参数 | 压缩文本不包含第二次完整长输出；runtime 日志只包含摘要/fingerprint，不打印完整输出或敏感值 |

### D. 原需求测试条目追踪

此表用于防止执行 agent 只选择容易的用例。原需求第 14 项属于 Phase 2，不计入本次 Phase 1
通过率，也不能提前预期阻止调用。

| 原需求测试要求 | 本流程覆盖项 |
| --- | --- |
| 1. 参数字典顺序不同但 fingerprint 相同 | A1 |
| 2. 工具名或参数不同不会误判 | A2 |
| 3. 第二次相同结果生成压缩输出 | A3、B1 |
| 4. 相同调用但结果变化时连续计数重置 | A4、B10 |
| 5. `begin_run()` 后强制计数清零 | A5、B6、C8 |
| 6. Bash 重复调用仍真实执行 | B7 |
| 7. Phase 1 任何工具都不会被自动跳过 | A3、B1、B7 |
| 8. 原始结果进入 UI，压缩结果进入 `ToolResultBlock` | B1、B8 |
| 9. 流式、交互和 `run_to_completion()` 行为一致 | B3-B6 |
| 10. 同批并发相同调用不破坏计数或遗留 `in_flight` | A7、A9、C6、C9 |
| 11. 工具异常、参数校验失败、权限拒绝不泄漏状态 | A10、C1-C5 |
| 12. 超过最大条目数后按 LRU 淘汰 | A6、A7、C9 |
| 13. Subagent 之间 Tracker 状态隔离 | C7、C8 |
| 14. 只有 `GUARD` 工具能被阻止 | Phase 2，当前明确不执行 |

### E. 全量回归

完整矩阵通过后，仍须在同一个 `$lab` 隔离环境中运行项目全量测试。它用于发现 Tracker 接入三条
执行路径后对现有 Agent、工具、权限、Hook、Session、Job 和上下文逻辑造成的回归。

```powershell
Push-Location $lab
try {
    python -m pytest -p no:cacheprovider --basetemp "$lab\pytest-basetemp-all"
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 1 全量回归失败，exit=$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
```

验收规则：所有非预期失败和错误都必须为零；skip 只有在对应测试明确声明环境条件时才可接受。
报告需记录总 passed/failed/error/skipped 数量。不能用定向测试通过替代全量回归，也不能为了通过
而删除、跳过或改写原有测试。

## 5. Agent 何时会反复读取同一文件或调用同一工具

测试数据应模拟真实的循环原因，而不是只重复字符串。以下场景都用同一任务内、同一工具名、
同一最终参数开始；只有“结果是否变化”决定是否进入连续重复计数。

### 5.1 同一文件读取

- 模型需要确认前后文，但没有利用第一次 `ReadFile(path, offset, limit)` 的结果。
- 文件路径错误、路径大小写/工作目录理解错误，模型重复读取同一错误路径。
- 工具返回空内容或固定的“不存在/无权限”错误，模型没有改查找策略。
- 修改前后没有重新计算 offset/limit，模型重复读取同一片段。
- 模型在等待外部状态时轮询同一文件；如果文件内容已经变化，result hash 变化，计数应重置，不能误报为无效循环。

以下不算同一调用：路径、offset、limit、编码或其他参数任一变化；PreTool Hook 改写后的最终参数不同；
新用户任务或另一个 Agent。

### 5.2 同一工具调用

- `Bash` 相同命令被模型连续重发；即使输出相同，Phase 1 仍必须真实执行。
- `Grep`/`Glob` 查询相同且目录未变化。
- `EditFile`/`WriteFile` 因模型没有确认写入结果而重复提交；Phase 1 不能把副作用结果当缓存返回。
- `ToolSearch` 对同一查询反复尝试但工具目录没有变化。
- 参数字段顺序变化、每次 `tool_use_id` 变化不应阻止判定为同一 fingerprint。

以下情况应允许继续执行并在必要时重置计数：结果正文变化、`is_error` 变化、参数变化、Hook 最终参数变化、
新 run、不同 Agent，以及同一批次尚未完成的并发调用。

## 6. 执行顺序

按以下顺序执行，失败时停止扩展测试并保留临时实验日志：

1. 建立临时副本和环境变量，记录源项目 `git status`。
2. 先跑 A 组纯 Tracker 单元测试，确认指纹、结果 hash、计数和容量基础可靠。
3. 再跑 B 组 Agent 路径测试，确认真实执行、压缩内容、UI/Conversation 分流和 runtime 事件。
4. 再跑 C 组错误、取消、并发、隔离和边界测试。
5. 运行 E 组全量回归，验证现有子系统没有行为退化。
6. 对每个矩阵项记录 `PASS`、`FAIL` 或 `LIMITATION`，附最小复现序列和观察值。
7. 删除临时实验目录，比较源项目前后 `git status`，确认没有生成物。

如果某一项只能通过直接调用内部方法验证，应在报告中明确标注“单元层通过”，不能把它写成已完成端到端验证。

## 7. 测试报告模板

将报告写到临时目录，确认无误后只复制最终 Markdown 报告到约定位置；不要复制日志、缓存或二进制文件。

```text
# Phase 1 测试报告

- 日期：
- 源项目路径：
- 临时实验目录：
- Python / pytest 版本：
- 测试 agent：
- 源项目 git status 前后是否一致：是 / 否
- 临时目录是否删除：是 / 否

## 结果摘要

- A 组：通过 __ / __，失败 __，限制 __
- B 组：通过 __ / __，失败 __，限制 __
- C 组：通过 __ / __，失败 __，限制 __
- 全量回归：passed __，failed __，errors __，skipped __
- 工具真实执行次数是否符合调用次数：是 / 否
- 是否出现 Tracker 状态泄漏：是 / 否

## 矩阵记录

| ID | 输入序列 | 工具 calls | same_result_count | in_flight | UI/Conversation 断言 | runtime 事件 | 结论 |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| A1 |  |  |  |  |  |  |  |

## 失败或限制

每项只写复现步骤、实际值、预期值和是否阻塞 Phase 2；不要粘贴完整敏感输出。
```

## 8. Phase 2 修改前的门槛

满足以下条件后，才可以开始 Phase 2 的 `GUARD` 设计和测试：

- A、B、C 组没有未解释的失败；
- B3、B4、B5、B6 四条执行路径均有证据；
- B1/B8 证明“工具真实执行”和“Conversation 压缩”同时成立；
- C1-C7 证明错误、权限、取消、并发、Subagent 不会泄漏状态；
- C9 的容量行为已明确：活动记录可短暂超过容量，收尾后必须裁剪，或已完成硬上限修复；
- E 组全量回归没有失败或错误，所有 skip 均有明确环境原因；
- 源项目状态前后一致，临时目录已删除；
- 报告中没有把 Phase 2 的 `GUARD` 阻止行为混入 Phase 1 结论。

Phase 2 仅另行验证：`ReadFile`、`Glob`、`Grep`、`ToolSearch` 等显式 `GUARD` 工具在连续相同结果后
阻止下一次执行；`Bash`、`EditFile`、`WriteFile` 和默认插件/MCP 仍不能因重复而自动复用。Phase 1 测试
不应预期 `block_next=True` 或 `tool_loop_guarded` 事件。

## 9. 可直接交给测试 agent 的任务说明

先由操作者按第 2 节创建 `$lab` 并设置隔离环境，再把下面文字交给测试 agent。把实际 `$lab`
路径替换进提示，不要把源项目目录作为 agent 的工作目录。

```text
你正在验证 Braincode RecentToolCallTracker Phase 1。

权限边界：
1. 只允许读取和修改 <LAB_PATH>；禁止修改 E:\py_project\Braincode。
2. 不允许调用真实网络、真实 LLM、真实 Shell/Bash 工具、数据库或项目中的真实写文件工具。
3. 所有数据必须使用确定性的 fake Tool、fake LLM client、fake Hook、fake PermissionChecker 和临时目录。
4. 不修复产品源码。若发现缺陷，只写最小复现、实际值、预期值和 Phase 2 阻塞结论。

任务：
1. 完整阅读 <LAB_PATH>\docs\ch1\phase1-test-process.md。
2. 先运行现有 tests/test_recent_tool_calls.py 和 tests/test_recent_tool_calls_agent.py，作为基线。
3. 在 <LAB_PATH>\tests\phase1_matrix_virtual_test.py 中实现第 4 节 A1-A10、B1-B10、C1-C10。
4. 每个测试使用矩阵给定的虚拟数据；必须断言 fake tool calls、same_result_count、in_flight、
   UI/Conversation 内容和 runtime 事件，不得只断言“未抛异常”。
5. 运行下面的完整矩阵命令，然后按第 4 节 E 组命令运行项目全量回归。
6. 把报告写入 <LAB_PATH>\phase1-report.md，逐项标记 PASS、FAIL 或 LIMITATION，并记录全量统计。
7. 直接调用内部方法的测试标为“单元层”，不得冒充端到端。
8. 返回报告摘要和失败测试名，不要复制缓存、Session、SQLite、完整敏感输出或二进制文件。
```

测试 agent 创建临时矩阵文件后，在 `$lab` 中执行：

```powershell
$targets = @(
    'tests/test_recent_tool_calls.py',
    'tests/test_recent_tool_calls_agent.py',
    'tests/phase1_matrix_virtual_test.py'
)
if (-not (Test-Path $targets[2])) {
    throw '完整矩阵临时测试文件不存在，不能把基线测试当成完整验收。'
}
python -m pytest -p no:cacheprovider --basetemp "$lab\pytest-basetemp-full" -vv @targets
if ($LASTEXITCODE -ne 0) {
    throw "Phase 1 完整矩阵失败，exit=$LASTEXITCODE"
}
```

报告被人工检查并提取摘要后，按第 2 节删除整个 `$lab`。由于矩阵测试文件只存在于临时副本，
无论测试通过或失败，都不会成为源项目改动；失败时应先从报告提取最小复现，再删除实验目录。
