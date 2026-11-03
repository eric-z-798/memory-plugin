"""Explainable model selection without a classifier request or quota assumptions.

The score is a local heuristic, not a measure of model intelligence or token cost.
It combines task, input length, structure, code and reasoning signals. Profiles
are user-editable model IDs; recommendations do not imply account availability.
Manual settings preserve the old CLI-default behavior, including an empty model.
"""

import copy
import re

from .common import MEMBER_ORDER


TIERS = ("fast", "balanced", "deep")
_PROFILES = {
    "claude": {"fast": "haiku", "balanced": "sonnet", "deep": "opus"},
    "gpt": {"fast": "gpt-6-luna", "balanced": "gpt-6.1-sol", "deep": "gpt-6-astra"},
    # `agy models` is the authority for the models available to this account.
    "gemini": {"fast": "", "balanced": "", "deep": ""},
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]*(?:\[[1-9][0-9]*[kKmM]\])?$")
_TIER_NAMES = {"fast": "轻量", "balanced": "均衡", "deep": "深入"}


def _member(member):
    if member not in MEMBER_ORDER:
        raise ValueError("未知成员：%s" % member)


def _model(value, label):
    if not isinstance(value, str):
        raise ValueError("%s 必须是模型名称字符串" % label)
    value = value.strip()
    if value and (len(value) > 200 or not _MODEL_ID.fullmatch(value)):
        raise ValueError("%s 应为不含空白、长度不超过 200 的模型 ID 或别名" % label)
    return value


def default_routing(member):
    """Return independent defaults; automatic routing is opt-in."""
    _member(member)
    return {"mode": "manual", "profiles": dict(_PROFILES[member])}


def clean_routing(member, value):
    """Normalize one complete routing config. Omitted fields use defaults."""
    _member(member)
    if not isinstance(value, dict) or set(value) - {"mode", "profiles"}:
        raise ValueError("模型路由设置必须包含 mode 和 profiles")
    mode = value.get("mode", "manual")
    if mode not in ("manual", "auto"):
        raise ValueError("模型路由模式只能是 manual 或 auto")
    profiles = value.get("profiles", {})
    if not isinstance(profiles, dict) or set(profiles) - set(TIERS):
        raise ValueError("模型档位只能包含 fast、balanced、deep")
    result = default_routing(member)
    result["mode"] = mode
    for tier, model in profiles.items():
        result["profiles"][tier] = _model(model, _TIER_NAMES[tier] + "模型")
    if mode == "auto" and not all(result["profiles"].values()):
        raise ValueError("%s 自动切换前请填写轻量、均衡、深入三档模型" % member)
    return result


def clean_overrides(value):
    """Missing member means inherit; an empty manual model means CLI default."""
    if not isinstance(value, dict) or set(value) - set(MEMBER_ORDER):
        raise ValueError("主持人模型设置包含未知成员或格式不正确")
    result = {}
    for member, override in value.items():
        if not isinstance(override, dict) or set(override) - {"mode", "model"}:
            raise ValueError("%s 主持人模型设置格式不正确" % member)
        if override.get("mode") not in ("manual", "auto"):
            raise ValueError("主持人模型模式只能是 manual 或 auto；继承时请移除覆盖项")
        result[member] = {
            "mode": override["mode"],
            "model": _model(override.get("model", ""), "主持人指定模型"),
        }
    return result


def _complexity(task, query, context_chars):
    if not isinstance(query, str):
        raise ValueError("待分析的问题必须是字符串")
    if isinstance(context_chars, bool) or not isinstance(context_chars, int) or context_chars < 0:
        raise ValueError("上下文字符数必须是非负整数")
    if not isinstance(task, str):
        raise ValueError("讨论任务必须是字符串")
    text = query.strip()
    lower = text.lower()
    score, signals = 0, []

    def add(points, explanation):
        nonlocal score
        score += points
        signals.append(explanation)

    if len(text) >= 2000:
        add(2, "问题较长（至少 2000 字符）")
    elif len(text) >= 600:
        add(1, "问题包含较多信息（至少 600 字符）")
    if context_chars >= 50000:
        add(2, "输入上下文较长（至少 50000 字符）")
    elif context_chars >= 20000:
        add(1, "输入上下文较多（至少 20000 字符）")
    if task in ("review", "debate"):
        add(1, "讨论处于交叉评审或辩论阶段")
    if re.search(r"```|\b(?:def|class|function)\s+\w+|Traceback \(most recent call last\)", text):
        add(2, "问题包含代码或错误堆栈")
    if re.search(r"形式化证明|数学证明|定理证明|证明.{0,32}(?:定理|猜想|恒等式|不等式)|威胁建模|formal proof|prove (?:that|the)|threat model", lower):
        add(4, "涉及证明或威胁建模")
    elif re.search(r"架构|根因|复杂度|迁移方案|并发|一致性|权衡|architecture|root cause|trade[ -]?offs?|concurren|migration plan", lower):
        add(2, "涉及设计、根因或多项权衡")
    if re.search(r"资金安全|数据丢失|不可逆|安全审计|生产事故|data loss|irreversible|security audit|production incident", lower):
        add(2, "包含需要更仔细核查的后果或风险")
    steps = len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s*\S", text))
    constraints = len(re.findall(r"必须|不得|不能|至少|至多|\bmust\b|\bwithout\b|\bat least\b", lower))
    if steps >= 4 or constraints >= 3:
        add(1, "包含多项步骤或约束")
    # A moderate unstructured question should not be treated as a short lookup.
    if not score and len(text) > 160:
        add(1, "问题超过短问答长度（160 字符）")
    if not signals:
        signals.append("短问题、未识别到额外复杂信号" if task not in ("vote", "summary")
                       else "短输入的投票或摘要任务")
    tier = "deep" if score >= 4 else "balanced" if score >= 1 else "fast"
    return tier, {"method": "heuristic-v1", "score": score, "signals": signals,
                  "query_chars": len(text), "context_chars": context_chars}


def resolve_route(member, settings, group, task, query, context_chars=0):
    """Select one request model without mutating state or calling any service."""
    _member(member)
    if not isinstance(settings, dict) or not isinstance(group, dict):
        raise ValueError("模型选择需要设置与群配置")
    members = settings.get("members", {})
    if not isinstance(members, dict) or not isinstance(members.get(member, {}), dict):
        raise ValueError("成员设置格式不正确")
    conf = members.get(member, {})
    override = clean_overrides(group.get("model_overrides", {})).get(member)
    # Validate the profiles separately from the mode: a manual host choice must
    # remain usable even if an inherited automatic configuration is incomplete.
    raw = conf.get("routing", default_routing(member))
    if not isinstance(raw, dict):
        raise ValueError("模型路由设置格式不正确")
    inherited_mode = raw.get("mode", "manual")
    if inherited_mode not in ("manual", "auto"):
        raise ValueError("模型路由模式只能是 manual 或 auto")
    mode = override["mode"] if override else inherited_mode
    routing = clean_routing(member, dict(raw, mode=mode))
    tier, complexity = _complexity(task, query, context_chars)
    source = "group_override" if override else "member_setting"
    if mode == "manual":
        model = override["model"] if override else _model(conf.get("model", ""), "固定模型")
        reason = "主持人手动指定" if override else "沿用成员的固定模型设置"
        if not model:
            reason += "；使用 CLI 默认模型，具体型号由 CLI 决定"
        tier = "manual"
    else:
        model = routing["profiles"][tier]
        reason = "本地规则选择%s档：%s" % (_TIER_NAMES[tier], "；".join(complexity["signals"]))
    return {"mode": mode, "source": source, "tier": tier,
            "requested_model": model, "reason": reason, "complexity": complexity}


def route_settings(settings, member, route):
    """Create a call-scoped snapshot; never overwrite the user's fixed model."""
    _member(member)
    if not isinstance(settings, dict) or not isinstance(route, dict):
        raise ValueError("模型调用配置格式不正确")
    model = _model(route.get("requested_model", ""), "请求模型")
    result = copy.deepcopy(settings)
    if not isinstance(result.get("members", {}), dict):
        raise ValueError("成员设置格式不正确")
    conf = result.setdefault("members", {}).setdefault(member, {})
    if not isinstance(conf, dict):
        raise ValueError("成员设置格式不正确")
    conf["model"] = model
    return result
