# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""Braincode 的配置校验逻辑。"""

from __future__ import annotations

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

DEFAULT_CONTEXT_WINDOW = 200_000

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


class ConfigError(Exception):
    pass


def validate_providers(raw_providers: list) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。"""
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        raise ConfigError("At least one provider must be configured")

    providers: list[dict] = []
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"Provider #{i + 1}: missing fields: {', '.join(missing)}")

        protocol = entry["protocol"]
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"Provider #{i + 1}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"Provider #{i + 1}: context_window must be a positive integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"Provider #{i + 1}: thinking must be a boolean")

        max_output_tokens = entry.get("max_output_tokens", 0)
        if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise ConfigError(
                f"Provider #{i + 1}: max_output_tokens must be a non-negative integer"
            )

        providers.append(
            {
                "name": entry["name"],
                "protocol": protocol,
                "base_url": entry["base_url"],
                "model": entry["model"],
                "api_key": entry.get("api_key", ""),
                "thinking": thinking,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    return providers


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


def validate_mcp_servers(raw_mcp: list | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        name = entry.get("name")
        if not name:
            raise ConfigError(f"MCP server #{i + 1}: missing 'name'")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        servers.append(
            {
                "name": name,
                "command": entry.get("command"),
                "args": entry.get("args", []),
                "url": entry.get("url"),
                "headers": entry.get("headers", {}),
                "env": entry.get("env", {}),
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    return raw_hooks


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", ".venv", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


def validate_sandbox(raw_sb: dict | None) -> dict:
    """校验 sandbox 配置段，返回清洗后的配置字典。"""
    defaults = {
        "enabled": False,
        "auto_allow": False,
        "network_enabled": False,
    }

    if raw_sb is None:
        return defaults

    if not isinstance(raw_sb, dict):
        raise ConfigError("'sandbox' must be a mapping")

    result = dict(defaults)
    for key in ("enabled", "auto_allow", "network_enabled"):
        if key in raw_sb:
            val = raw_sb[key]
            if not isinstance(val, bool):
                raise ConfigError(f"'sandbox.{key}' must be a boolean")
            result[key] = val

    return result


def validate_recovery(raw_recovery: dict | None) -> dict:
    defaults = {
        "max_retries": 6,
        "base_delay_seconds": 0.5,
        "max_delay_seconds": 32.0,
        "max_output_continuations": 3,
        "fallback_providers": [],
    }
    if raw_recovery is None:
        return defaults
    if not isinstance(raw_recovery, dict):
        raise ConfigError("'recovery' must be a mapping")
    result = dict(defaults)
    max_retries = raw_recovery.get("max_retries", defaults["max_retries"])
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ConfigError("'recovery.max_retries' must be a non-negative integer")
    result["max_retries"] = max_retries
    for key in ("base_delay_seconds", "max_delay_seconds"):
        value = raw_recovery.get(key, defaults[key])
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"'recovery.{key}' must be a non-negative number")
        result[key] = float(value)
    if result["max_delay_seconds"] < result["base_delay_seconds"]:
        raise ConfigError(
            "'recovery.max_delay_seconds' must be >= base_delay_seconds"
        )
    continuations = raw_recovery.get(
        "max_output_continuations", defaults["max_output_continuations"]
    )
    if not isinstance(continuations, int) or isinstance(continuations, bool) or continuations < 0:
        raise ConfigError(
            "'recovery.max_output_continuations' must be a non-negative integer"
        )
    result["max_output_continuations"] = continuations
    fallbacks = raw_recovery.get("fallback_providers", [])
    if not isinstance(fallbacks, list) or not all(
        isinstance(value, str) and value.strip() for value in fallbacks
    ):
        raise ConfigError("'recovery.fallback_providers' must be a list of names")
    if len(set(fallbacks)) != len(fallbacks):
        raise ConfigError("'recovery.fallback_providers' must not contain duplicates")
    result["fallback_providers"] = fallbacks
    return result


def validate_scheduler(raw_scheduler: dict | None) -> dict:
    defaults = {
        "enabled": False,
        "timezone": "UTC",
        "poll_interval_seconds": 1.0,
        "default_misfire_policy": "skip",
        "default_overlap_policy": "coalesce",
    }
    if raw_scheduler is None:
        return defaults
    if not isinstance(raw_scheduler, dict):
        raise ConfigError("'scheduler' must be a mapping")
    result = dict(defaults)
    enabled = raw_scheduler.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("'scheduler.enabled' must be a boolean")
    result["enabled"] = enabled
    timezone = raw_scheduler.get("timezone", defaults["timezone"])
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("'scheduler.timezone' must be a timezone name")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown scheduler timezone: {timezone}") from exc
    result["timezone"] = timezone
    interval = raw_scheduler.get(
        "poll_interval_seconds", defaults["poll_interval_seconds"]
    )
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("'scheduler.poll_interval_seconds' must be positive")
    result["poll_interval_seconds"] = float(interval)
    misfire = raw_scheduler.get(
        "default_misfire_policy", defaults["default_misfire_policy"]
    )
    if misfire not in {"skip", "run_once"}:
        raise ConfigError("Invalid scheduler.default_misfire_policy")
    result["default_misfire_policy"] = misfire
    overlap = raw_scheduler.get(
        "default_overlap_policy", defaults["default_overlap_policy"]
    )
    if overlap not in {"skip", "coalesce", "parallel"}:
        raise ConfigError("Invalid scheduler.default_overlap_policy")
    result["default_overlap_policy"] = overlap
    return result


def validate_config_structure(raw: object) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode、sandbox
    """
    if not isinstance(raw, dict) or "providers" not in raw:
        raise ConfigError("Config must contain a 'providers' list")

    providers = validate_providers(raw["providers"])
    recovery = validate_recovery(raw.get("recovery"))
    scheduler = validate_scheduler(raw.get("scheduler"))
    provider_names = {provider["name"] for provider in providers}
    unknown_fallbacks = [
        name for name in recovery["fallback_providers"] if name not in provider_names
    ]
    if unknown_fallbacks:
        raise ConfigError(
            "Unknown recovery fallback provider(s): "
            + ", ".join(unknown_fallbacks)
        )

    return {
        "providers": providers,
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
        "sandbox": validate_sandbox(raw.get("sandbox")),
        "recovery": recovery,
        "scheduler": scheduler,
    }
