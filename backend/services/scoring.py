# 评分引擎：模板加载 / 规则书渲染 / 输出侧 guardrails（熔断与总分由后端重算，不信任模型算术）
from sqlalchemy.orm import Session

from backend.config import DEFAULT_TEMPLATES
from backend.db import repository
from backend.schemas.inspection import LLMResultSchema
from backend.services import parser as parser_service

D_TOTAL = 55
S_TOTAL = 45

# S 维度各子项说明（规则书用，与模板配置的权重数值互补）
S_DIM_DESCRIPTIONS = {
    "s1": "安抚并稳定客户情绪：共情（感同身受的回应）、定制化（针对该客户情况的专属安抚）、直接（不回避正面回应）、无冲突（不与客户争辩、不反驳客户）、宣泄引导（引导客户把情绪说出来而不是憋着）。",
    "s2": "问题是否完整闭环：完整性（客户提出的问题全部有回应）、结构化（结论+原因+下一步的三段式表达）、下一步动作（给出明确可执行的下一步）、跟进承诺（承诺后续跟进的时间与方式）。",
    "s3": "专业供给质量：逻辑（分析推理严密无漏洞）、讲原因（解释为什么，而非只给结论）、决策归属（把最终决策权交还客户，不替客户做主）。",
}

_D_KEYS = ["d1", "d2", "d3", "d4"]
_S_KEYS = ["s1", "s2", "s3"]


def load_template(session: Session, template_type: str) -> dict:
    """模板配置：优先 score_templates 表，缺失回退内置默认。"""
    row = repository.get_template(session, template_type)
    if row is not None:
        import json

        try:
            config = json.loads(row.config_json)
            if config:
                return config
        except json.JSONDecodeError:
            pass
    return DEFAULT_TEMPLATES.get(template_type) or DEFAULT_TEMPLATES["standard"]


def render_rulebook(template: dict) -> str:
    """把模板配置渲染为 markdown 规则书，注入系统提示词。"""
    lines = [f"## 质检评分规则书（{template['name']}）", ""]
    threshold = int(template.get("yellow_threshold", 59))
    lines.append(
        f"总分构成：D端 {D_TOTAL} 分 + S端 {S_TOTAL} 分，满分 100 分。"
    )
    lines.append(
        f"**黄灯熔断机制**：总分 < {threshold} 分时，is_yellow_alert 必须为 true，"
        f"并在 yellow_alert_reasons 中给出 1~3 条最严重的失分根因。"
        "禁止为了规避黄灯而虚高打分，违者视为质检事故。"
    )
    lines.append("")
    lines.append(f"### D端（{D_TOTAL} 分）")
    for key in _D_KEYS:
        dim = template["d"].get(key)
        if not dim:
            continue
        lines.append(f"#### {key.upper()} {dim['name']}（满分 {dim['max']} 分，权重 {dim['weight']}%）")
        lines.append(f"- 评分要点：{dim['anchors']}")
        lines.append(f"- 评分锚点：{dim['ratings']}")
        lines.append("")
    lines.append(f"### S端（{S_TOTAL} 分）")
    for key in _S_KEYS:
        dim = template["s"].get(key)
        if not dim:
            continue
        subs = "、".join(
            f"{item['name']} {item['max']} 分" for item in dim["sub_items"].values()
        )
        lines.append(f"#### {key.upper()} {dim['name']}（满分 {dim['max']} 分）子项：{subs}")
        lines.append(f"- 评分要点：{S_DIM_DESCRIPTIONS.get(key, '')}")
        lines.append("")
    return "\n".join(lines)


def _d_items(result: LLMResultSchema):
    return [
        ("d1", result.d_scores.d1_emotion_change),
        ("d2", result.d_scores.d2_profile_match),
        ("d3", result.d_scores.d3_problem_match),
        ("d4", result.d_scores.d4_expectation_exceed),
    ]


def _s_items(result: LLMResultSchema):
    return [
        ("s1", result.s_scores.s1_emotion_stabilize),
        ("s2", result.s_scores.s2_problem_closure),
        ("s3", result.s_scores.s3_professional_supply),
    ]


def apply_guardrails(result: LLMResultSchema, template: dict, turn_count: int) -> LLMResultSchema:
    """输出侧校核：越界钳制、S 维度分=Σ子项（算术剥离）、总分与熔断重算、红灯补全、建议裁剪、高亮过滤排序。"""
    # D 端：分数钳制到 [0, max]（analysis 为思维链文本，原样保留）
    for key, item in _d_items(result):
        dim_max = int(template["d"].get(key, {}).get("max", 0))
        item.score = max(0, min(int(item.score), dim_max))
    # S 端：子项钳制；维度分 = Σ子项（模型给的值被覆盖——算术由后端接管，不信任模型汇总）
    for key, item in _s_items(result):
        dim = template["s"].get(key, {})
        dim_max = int(dim.get("max", 0))
        sub_scores = []
        for sub_key, sub_conf in (dim.get("sub_items") or {}).items():
            sub_item = getattr(item.sub_items, sub_key, None)
            if sub_item is None:
                continue
            sub_item.score = max(0, min(int(sub_item.score), int(sub_conf.get("max", 0))))
            sub_scores.append(sub_item.score)
        item.score = min(dim_max, sum(sub_scores))
    # 总分与熔断强制重算（不信任模型算术）
    d_total = sum(item.score for _, item in _d_items(result))
    s_total = sum(item.score for _, item in _s_items(result))
    result.total_score = d_total + s_total
    threshold = int(template.get("yellow_threshold", 59))
    result.is_yellow_alert = result.total_score < threshold
    if result.is_yellow_alert and not result.yellow_alert_reasons:
        losses = []
        for key, item in _d_items(result):
            dim_max = int(template["d"].get(key, {}).get("max", 0))
            losses.append((dim_max - item.score, key.upper(), template["d"][key]["name"], item.score, dim_max))
        for key, item in _s_items(result):
            dim_max = int(template["s"].get(key, {}).get("max", 0))
            losses.append((dim_max - item.score, key.upper(), template["s"][key]["name"], item.score, dim_max))
        losses.sort(reverse=True)
        result.yellow_alert_reasons = [
            f"{name}失分严重（{score}/{max_} 分）" for _, _, name, score, max_ in losses[:3]
        ]
    result.yellow_alert_reasons = result.yellow_alert_reasons[:3]
    # 红灯一票否决：触发但没给原因时补默认原因（reasons 裁剪 3 条）
    if result.is_red_alert:
        reasons = [r.strip() for r in result.red_alert_reasons if r and r.strip()]
        if not reasons:
            reasons = ["命中合规红线（承诺收益 / 代客理财 / 报具体点位），请人工复核该会话"]
        result.red_alert_reasons = reasons[:3]
    # 建议裁剪到 1~3 条，去掉空串
    suggestions = [s.strip() for s in result.improvement_suggestions if s and s.strip()]
    result.improvement_suggestions = suggestions[:3]
    # 高亮：过滤越界轮次、补角色、按轮次排序、最多 5 条
    highlights = []
    for h in result.highlight_dialogue:
        if not (1 <= int(h.turn) <= max(turn_count, 1)):
            continue
        role = parser_service.normalize_role(h.role) or "助"
        h.role = role
        highlights.append(h)
    highlights.sort(key=lambda h: h.turn)
    result.highlight_dialogue = highlights[:5]
    return result


def validate_template_config(config: dict) -> list[str]:
    """模板配置校验：D 和=55、S 和=45、阈值 0~100、子项和=维度满分。返回错误列表（空=通过）。"""
    errors: list[str] = []
    d = config.get("d") or {}
    s = config.get("s") or {}
    d_sum = 0
    for key in _D_KEYS:
        dim = d.get(key)
        if not dim:
            errors.append(f"缺少 D 端维度 {key}")
            continue
        try:
            dim_max = int(dim["max"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{key} 满分必须为整数")
            continue
        if dim_max <= 0:
            errors.append(f"{key} 满分必须大于 0")
        d_sum += dim_max
    if d and d_sum != D_TOTAL:
        errors.append(f"D 端满分合计 {d_sum}，必须等于 {D_TOTAL}")
    s_sum = 0
    for key in _S_KEYS:
        dim = s.get(key)
        if not dim:
            errors.append(f"缺少 S 端维度 {key}")
            continue
        try:
            dim_max = int(dim["max"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{key} 满分必须为整数")
            continue
        if dim_max <= 0:
            errors.append(f"{key} 满分必须大于 0")
        s_sum += dim_max
        subs = dim.get("sub_items") or {}
        sub_sum = 0
        if not subs:
            errors.append(f"{key} 缺少子项配置")
            continue
        for sub_key, sub_conf in subs.items():
            try:
                sub_max = int(sub_conf["max"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"{key}.{sub_key} 满分必须为整数")
                continue
            sub_sum += sub_max
        if sub_sum != dim_max:
            errors.append(f"{key} 子项满分合计 {sub_sum}，必须等于维度满分 {dim_max}")
    if s and s_sum != S_TOTAL:
        errors.append(f"S 端满分合计 {s_sum}，必须等于 {S_TOTAL}")
    try:
        threshold = int(config.get("yellow_threshold", -1))
        if not (0 <= threshold <= 100):
            errors.append("黄灯阈值必须为 0~100 的整数")
    except (TypeError, ValueError):
        errors.append("黄灯阈值必须为 0~100 的整数")
    return errors
