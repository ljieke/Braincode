# Phase 2 验证流程：只读工具 Guard

本文档用于验证 `RecentToolCallTracker` Phase 2 的只读工具 Guard 行为，并定义进入
Phase 3 前必须满足的门槛。

Phase 2 需要进一步验证。单元测试可以证明阈值、策略和状态机没有明显错误，但不能单独证明：

- 正常轮询、文件变化和工具发现流程不会被误判；
- 默认 `guard_after=4` 对真实任务是否合适；
- Guard 是否真的让模型改变策略，而不是持续重试；
- Guard 带来的上下文节省是否值得它引入的行为风险。

因此验证分为三层：确定性自动化测试、实际工具加虚拟数据测试、受控真实任务观察。
Phase 3 也应拆成两个步骤：

```text
Phase 2 确定性验证通过
-> Phase 3A：实现统计和可观测性
-> 收集并人工分类真实样本
-> Phase 3B：调整阈值、策略或开放配置
```

确定性测试通过后可以开始 Phase 3A；没有足够真实样本时，不应直接修改默认阈值或扩大
`GUARD` 工具范围。

## 1. 当前行为基线

同一个 fingerprint 在同一 run 中连续得到相同最终结果时，默认行为如下：

| 调用 | `GUARD` 工具 | `WARN` / `OBSERVE` 工具 | 预期状态 |
| --- | --- | --- | --- |
| 第 1 次 | 执行 | 执行 | `same_result_count=1` |
| 第 2 次 | 执行 | 执行 | 压缩 Conversation 结果 |
| 第 3 次 | 执行 | 执行 | 返回循环警告 |
| 第 4 次 | 执行 | 执行 | `block_next=True` |
| 第 5 次 | 阻止一次 | 继续执行 | 仅 `GUARD` 产生 `tool_loop_guarded` |
| 第 6 次 | 重新允许执行 | 继续执行 | 用于重新观察外部状态；结果未变则再次 armed |

Guard 是一次性的，不是永久缓存。被阻止的调用：

- 必须生成配对的合成 `tool_result`，`is_error=True`；
- 不执行 Pydantic 参数校验、工具本体、恢复快照和 PostTool Hook；
- 不调用 `after_call()`，不增加或遗留 `in_flight`；
- 不返回或复用上一次工具输出；
- 只记录 fingerprint 和计数，不记录完整参数或输出。

策略基线：

| 工具 | 策略 |
| --- | --- |
| `ReadFile`、`Glob`、`Grep`、`ToolSearch` | `GUARD` |
| `Bash`、`EditFile`、`WriteFile` | `WARN` |
| 未显式声明的 Tool、插件、MCP | `OBSERVE` |

## 2. 完成定义

Phase 2 验证完成必须同时满足：

1. 第 4 节自动化矩阵全部通过，全量回归没有非预期失败。
2. 只有 `GUARD` 工具能被阻止，前四次相同完成结果均真实执行。
3. `WARN`、`OBSERVE`、插件和 MCP 默认工具的执行次数始终等于模型调用次数。
4. 新 run、不同 fingerprint、结果或 `is_error` 变化不会继承错误的 Guard 判定。
5. 同批并发调用不会因兄弟调用先完成而被新产生的 Guard 误挡。
6. Guard 命中后 Provider 对话仍保持一一配对，没有缺失 `tool_result`。
7. 权限、Hook、校验、异常和取消路径没有 Tracker 状态泄漏。
8. 第 5 节实际工具测试通过，并完成第 6 节受控任务观察报告。
9. 没有高影响误报；所有低影响或不确定事件都有可复现分类。

## 3. 隔离和无污染要求

### 3.1 基本规则

- 自动化和虚拟数据测试复用 `phase1-test-process.md` 第 2 节的临时副本方案。
- 不在源项目直接创建 Session、SQLite、pytest cache、虚拟报告或测试夹具。
- 实际文件工具只能访问临时实验目录中的 fixture。
- 不对真实业务仓库执行 `WriteFile`、`EditFile` 或不受控 Bash。
- 受控真实任务使用专门的可丢弃仓库，不使用生产仓库或包含秘密的目录。
- 事件报告不得保存完整参数、文件正文、Shell 输出、token 或凭据。

### 3.2 建立临时实验副本

以下命令复制当前工作树，因此能包含尚未提交的 Phase 2 实现。测试结束必须使用
`phase1-test-process.md` 第 2.3 节的路径检查和清理流程。

```powershell
$repo = 'E:\py_project\Braincode'
$before = (git -C $repo status --porcelain=v1) -join "`n"
$id = [guid]::NewGuid().ToString('N')
$lab = Join-Path ([System.IO.Path]::GetTempPath()) "braincode-phase2-$id"
New-Item -ItemType Directory -Path $lab -Force | Out-Null

robocopy $repo $lab /E `
    /XD .git .braincode __pycache__ .pytest_cache .mypy_cache .ruff_cache .venv `
    /XF *.pyc *.pyo
if ($LASTEXITCODE -gt 7) {
    throw "临时副本复制失败，robocopy=$LASTEXITCODE"
}

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTEST_DEBUG_TEMPROOT = Join-Path $lab 'pytest-temp'
New-Item -ItemType Directory -Path $env:PYTEST_DEBUG_TEMPROOT -Force | Out-Null
```

测试完成后比较源目录状态：

```powershell
$after = (git -C $repo status --porcelain=v1) -join "`n"
if ($before -cne $after) {
    throw '源项目 git status 发生变化；保留差异排查，不要自动回滚。'
}
```

## 4. 确定性自动化验证

### 4.1 基线命令

在 `$lab` 中运行：

```powershell
Push-Location $lab
try {
    python -m pytest -p no:cacheprovider `
        --basetemp "$lab\pytest-phase2" -q `
        tests/test_recent_tool_calls.py `
        tests/test_recent_tool_calls_agent.py `
        tests/test_tool_repeat_policies.py `
        tests/test_runtime.py
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 2 定向测试失败，exit=$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
```

不能只记录 passed 总数。报告还必须逐项对应以下矩阵。

### 4.2 Tracker 状态机矩阵

| ID | 场景 | 关键断言 |
| --- | --- | --- |
| A1 | `GUARD`、相同参数和相同结果连续调用 5 次 | 前 4 次 `blocked=False` 且真实完成；第 4 次 `block_next=True`；第 5 次 `blocked=True` |
| A2 | A1 后进行第 6 次调用，并返回变化结果 | 第 6 次允许执行；`same_result_count` 重置为 1；不再 armed |
| A3 | `WARN` 和 `OBSERVE` 各连续调用至少 6 次 | 所有调用均执行；`block_next=False`；无 guarded 事件 |
| A4 | `guard_after=None` | `GUARD` 工具恢复 Phase 1 行为，任何次数均不阻止 |
| A5 | 输出正文或 `is_error` 变化 | 连续相同结果计数重置，旧 armed 状态清除 |
| A6 | 参数、工具名或 Hook 最终参数变化 | 使用不同 fingerprint，不误阻止当前调用 |
| A7 | `begin_run()` 后重复同一调用 | 新 run 首次调用正常执行，不继承 pending Guard |
| A8 | blocked 调用不调用 `after_call()` | `in_flight==0`，完成计数不增加，blocked attempt 的 call number 可观测 |
| A9 | 达到阈值前同时开始多个相同调用 | 所有未完成调用均执行；结果收齐后才可能 armed；无负数或状态泄漏 |
| A10 | 同批开始前未 armed，批内某个调用先完成并 armed | 同批兄弟调用不消费新 Guard；下一批调用才允许被阻止 |
| A11 | 同批开始前已经 armed，同 fingerprint 有多个兄弟调用 | 一次性 Guard 在该批最多消费一次；兄弟调用不会被重复消费同一 token |
| A12 | LRU 满、active overflow、abandon/cancel | active 记录不被错误淘汰；收尾后裁剪；`in_flight==0` |

### 4.3 Tool 策略矩阵

| ID | 场景 | 关键断言 |
| --- | --- | --- |
| P1 | 检查四个内置只读工具 | 均为 `RepeatPolicy.GUARD` |
| P2 | 检查 Bash、EditFile、WriteFile | 均为 `RepeatPolicy.WARN` |
| P3 | 未声明策略的 Tool 和插件 | 继承 `OBSERVE` |
| P4 | `MCPToolWrapper` | 默认 `OBSERVE` |
| P5 | 插件显式声明 `GUARD` | 显式策略保留且可生效 |
| P6 | 改变 `is_concurrency_safe` 或 `category` | 不会隐式改变 `repeat_policy` |

### 4.4 Agent 和生命周期矩阵

| ID | 场景 | 关键断言 |
| --- | --- | --- |
| B1 | `_execute_single_tool_direct()` 连续 5 次 | 工具执行 4 次，第 5 次得到合成 Guard 结果 |
| B2 | `_execute_tool()` 真实产生 PermissionRequest，用户 ALLOW | 先完成权限确认，再检查 Guard；5 次请求、4 次执行 |
| B3 | `_execute_tool_noninteractive()` 连续 5 次 | 返回类型不变；第 5 次阻止 |
| B4 | `run_to_completion()` 完整循环 | 五个 tool use 都有配对结果；工具只执行四次；下一次 run 重置 |
| B5 | `run()` 流式循环 | UI 前四次是实际输出；第五次是 Guard 合成结果；Conversation 配对正确 |
| B6 | PreTool Hook 把两个原始参数改成相同最终参数 | fingerprint 使用 Hook 后参数 |
| B7 | PostTool Hook 把不同原始输出改成相同结果 | result hash 使用模型最终可见输出；满足阈值后 Guard |
| B8 | PostTool Hook 保持输出不同 | 不错误 armed |
| B9 | Guard 命中 | 不运行参数校验、工具本体、恢复快照和 PostTool Hook |
| B10 | permission deny、Hook reject、校验失败、工具异常、取消 | 原有错误契约保持；无 phantom Guard 和 `in_flight` 泄漏 |
| B11 | 同一响应中多个相同调用，刻意让第一个先完成 | 本响应兄弟调用不被新 armed Guard 误挡 |
| B12 | 两个 Agent/Subagent | Tracker 和 Guard 状态完全隔离 |

### 4.5 Runtime 事件矩阵

默认相同结果序列应产生：

```text
第 2 次完成 -> tool_repeat_detected
第 3 次完成 -> tool_loop_warning
第 4 次完成 -> tool_loop_warning
第 5 次尝试 -> tool_loop_guarded（仅 GUARD）
```

每个事件检查：

- `agent_id`、`tool_name`、`fingerprint`、`call_count`、`same_result_count`、`policy` 存在；
- `policy` 是工具的实际策略，不是固定 `observe`；
- guarded 事件的默认计数为 `call_count=5`、`same_result_count=4`；
- payload、日志和异常文本不包含完整参数、输出或敏感值；
- `RuntimeEventBus` 能接受并分发 `tool_loop_guarded`，不会因枚举转换失败。

### 4.6 全量回归

定向矩阵通过后，在同一 `$lab` 运行：

```powershell
Push-Location $lab
try {
    python -m pytest -p no:cacheprovider `
        --basetemp "$lab\pytest-all" -q
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 2 全量回归失败，exit=$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
```

验收要求是 `failed=0`、`errors=0`。Skip 只能来自明确的环境条件；已有 warning 可以记录，
但新增 warning 必须排查。禁止通过删除、跳过或放宽旧测试来获得通过结果。

## 5. 实际工具加虚拟数据验证

这一层不使用真实 LLM，但要使用真实 `ReadFile`、`Glob`、`Grep`、`ToolSearch` 实现，数据只放在
`$lab\phase2-fixture`。通过 fake client 固定调用序列，通过 spy 包装 `execute()` 统计实际执行次数。

### 5.1 Fixture

```text
phase2-fixture/
  src/a.py       # 含两个可搜索标记
  src/b.py       # 初始固定内容
  notes/readme.md
```

不得读取源项目文件来制造相同结果，避免测试结果依赖当前工作树内容。

### 5.2 场景

| ID | 实际工具 | 操作 | 预期 |
| --- | --- | --- | --- |
| R1 | ReadFile | 同一路径、offset、limit 调用 5 次 | 前四次真实读取，第五次 Guard；不返回第四次正文 |
| R2 | ReadFile | R1 后修改 fixture，再进行第 6 次读取 | 第 6 次允许执行并看到新内容；结果变化后计数重置 |
| R3 | ReadFile | 文件不存在，重复同一错误 5 次 | 相同错误也参与计数；第五次 Guard；无异常泄漏 |
| R4 | Glob | 固定目录和 pattern 调用 5 次 | 前四次执行，第五次 Guard；结果列表不被当缓存返回 |
| R5 | Glob | 第 4 次后新增匹配文件，Guard 一次后再调用 | 下一次真实执行并看到新文件，证明 Guard 非永久缓存 |
| R6 | Grep | 固定 pattern/path/include 调用 5 次 | 默认阈值和事件正确 |
| R7 | Grep | 修改匹配行后重新执行 | 新结果重置连续计数 |
| R8 | ToolSearch | 固定 registry 和 query 调用 5 次 | 前四次执行，第五次 Guard；registry 状态无损坏 |
| R9 | Bash 名称的 deterministic fake tool | 相同调用 6 次 | 六次全部执行，仅 warning，不 Guard |
| R10 | 插件和 MCP wrapper fake | 相同调用 6 次 | 默认 `OBSERVE`，六次全部执行 |

R2、R5、R7 是进入真实观察前最重要的误报防护：它们证明一次 Guard 不会把读取类工具变成永久缓存。

## 6. 受控真实任务观察

### 6.1 观察范围

使用可丢弃代码仓库和正常 Braincode 交互，不强迫模型固定重复工具。建议最小观察窗口：

- 至少 30 个独立用户任务；
- 覆盖代码阅读、错误定位、搜索、测试分析、文件修改后复查、工具发现至少 5 类任务；
- 至少跨 3 个不同规模的 fixture 仓库；
- 至少观察 7 天，或直到获得 20 个经过人工分类的 guarded 事件；
- 如果 30 个任务后 guarded 事件少于 10 个，可以开始 Phase 3A，但不能据此调整默认阈值。

真实观察不能使用生产仓库、线上凭据或真实 MCP 写操作。插件/MCP 只选只读测试服务，或继续使用
本地 fake wrapper。

### 6.2 必含任务场景

1. 稳定文件或固定代码片段的重复读取。
2. `ReadFile` 之间有 `EditFile`/`WriteFile` 或外部 fixture 修改。
3. 正常轮询：等待测试日志、生成文件或异步状态发生变化。
4. 相同 `Glob`/`Grep` 查询没有变化，以及查询结果随后发生变化。
5. 重复 `ToolSearch` 查询。
6. 重复 Bash、写文件和插件调用，确认它们从不被 Guard。
7. 同一响应发出多个并发只读调用。
8. 新用户任务主动重复上一任务的读取，确认 `begin_run()` 生效。
9. 权限 ALLOW/DENY、Hook 修改参数、Subagent 独立运行。

### 6.3 每个 guarded 事件的人工分类

| 分类 | 定义 |
| --- | --- |
| 真阳性 TP | 模型在没有状态变化证据时重复同一观察，Guard 正确终止无效循环 |
| 低影响误报 FP-L | 调用有合理理由，但一次阻止后下一次重试即可恢复，任务未失败 |
| 高影响误报 FP-H | Guard 阻止必要观察，导致错误结论、任务失败、长期轮询失效或需要用户介入 |
| 不确定 U | 无法仅凭脱敏事件判断，需要复现或补充场景标签 |

不要把“结果相同”自动视为 TP。正常轮询即使多次结果相同，也可能是合理行为。

### 6.4 最小数据字段

只记录脱敏元数据：

```text
task_id_hash
agent_id_hash
tool_name
policy
fingerprint
call_count
same_result_count
event_type
task_category
guard 后两轮内是否再次调用相同 fingerprint
guard 后是否改变策略
任务是否完成
人工分类 TP / FP-L / FP-H / U
备注（禁止粘贴参数和输出正文）
```

### 6.5 指标

```text
guard_rate = tool_loop_guarded / GUARD 工具总尝试数
warning_to_guard_rate = tool_loop_guarded / tool_loop_warning
reviewed_false_positive_rate = (FP-L + FP-H) / 已人工分类 guarded 事件
strategy_change_rate = Guard 后两轮内未再次调用相同 fingerprint / guarded 事件
task_failure_rate = Guard 相关失败任务 / 含 guarded 事件的任务
post_guard_retry_rate = Guard 后两轮内再次调用相同 fingerprint / guarded 事件
```

Phase 2 当前事件不足以精确计算节省 token。Phase 3A 可以增加“原结果字符数”和“Conversation
合成结果字符数”的数值指标，但不能记录正文。没有这两个长度时，只报告事件次数，不伪造 token 节省量。

## 7. 进入 Phase 3 的门槛

### 7.1 可以开始 Phase 3A 的条件

以下条件全部满足后，可以实现统计、聚合和脱敏指标：

- 第 4 节定向矩阵和全量回归通过；
- 第 5 节实际工具场景全部通过；
- 没有非 `GUARD` 工具被阻止；
- 没有前四次提前阻止、永久阻止、对话配对错误或 `in_flight` 泄漏；
- Runtime 事件载荷完整且不泄露敏感信息；
- 至少完成一轮受控真实任务冒烟并建立人工分类流程。

### 7.2 可以开始 Phase 3B 调参的条件

以下条件全部满足后，才允许改变 `guard_after`、增加工具级覆盖或开放配置：

- 至少 30 个真实任务和 20 个已人工分类的 guarded 事件；
- `FP-H=0`；若出现过 FP-H，必须先有可复现测试和针对性设计，再讨论新默认值；
- 每个 `FP-L` 和 `U` 都有场景说明，不能从分母中静默删除；
- `WARN`、`OBSERVE` 的 guarded 数量始终为 0；
- Guard 后模型改变策略、继续任务或明确报告阻塞，不能大量无意义重试；
- 数据覆盖文件变化、正常轮询、并发、权限、Hook、新 run 和 Subagent；
- 报告明确区分“增加可观测性”和“改变产品行为”。

数据不足时允许继续收集和完善 Phase 3A，不允许仅凭少量成功案例降低阈值或扩大 `GUARD` 范围。

### 7.3 必须退回 Phase 2 修复的情况

出现以下任一项时，停止 Phase 3 行为调参：

- `WARN`、`OBSERVE`、默认插件或 MCP 被阻止；
- 第 1-4 次相同结果调用被提前阻止；
- 新 run 继承旧 Guard；
- 同批并发调用被批内新完成结果误挡；
- Guard 后缺少 tool result，导致 Provider 协议错误；
- Guard 命中仍运行工具或 PostTool Hook；
- 状态泄漏、负 `in_flight`、事件敏感信息泄漏；
- 受控任务出现可复现的高影响误报。

## 8. 报告模板

```text
# Phase 2 验证报告

- 日期：
- commit / 工作树标识：
- Python / pytest 版本：
- 临时实验目录：
- 源项目 git status 前后是否一致：是 / 否
- 临时目录是否删除：是 / 否

## 自动化结果

- Tracker 矩阵：通过 __ / __
- Tool 策略矩阵：通过 __ / __
- Agent 矩阵：通过 __ / __
- Runtime 事件矩阵：通过 __ / __
- 全量回归：passed __，failed __，errors __，skipped __，warnings __

## 实际工具结果

| ID | 工具 | 尝试次数 | 真实执行次数 | guarded 次数 | 结果变化是否重置 | 结论 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| R1 | ReadFile | 5 | 4 | 1 | N/A | PASS |

## 真实任务观察

- 任务数：
- GUARD 工具尝试数：
- repeat / warning / guarded 事件数：
- TP / FP-L / FP-H / U：
- guard_rate：
- reviewed_false_positive_rate：
- strategy_change_rate：
- task_failure_rate：

## 失败、限制和复现

只记录场景、脱敏输入类别、实际行为、预期行为和影响等级，不粘贴完整参数或输出。

## 决策

- Phase 2 是否通过：是 / 否
- 是否允许进入 Phase 3A：是 / 否
- 是否允许进入 Phase 3B：是 / 否
- 阻塞项：
```

## 9. 推荐执行顺序

1. 创建临时实验副本并记录源项目状态。
2. 运行第 4.1 节定向测试。
3. 对照 A、P、B 和 Runtime 矩阵补齐任何缺失场景。
4. 运行第 4.6 节全量回归。
5. 使用真实内置工具和 fixture 执行 R1-R10。
6. 清理临时副本并确认源项目状态未变化。
7. 在可丢弃仓库执行受控真实任务观察，逐个分类 guarded 事件。
8. 输出第 8 节报告并做门槛判断。
9. 自动化和实际工具验证通过后可进入 Phase 3A。
10. 真实样本达到第 7.2 节门槛后，才进入 Phase 3B 调参。

