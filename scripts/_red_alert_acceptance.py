# v2 提示词重构验收：红灯一票否决 + 算术剥离 + analysis 思维链 + 老师腔改写（真实 LLM）
# 用例：员工话术含承诺收益 + 报具体点位 + 强指令 → 必须 is_red_alert=true，改写不得再报点位。
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8321"
API_KEY = sys.argv[1] if len(sys.argv) > 1 else ""

passed = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        sys.exit(1)
    passed.append(name)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=240)
    for m in c.get("/api/settings/models").json()["models"]:
        if m["name"] == "DeepSeek 红灯验收":
            c.delete(f"/api/settings/models/{m['id']}")

    # 1. 配置模型（含老师人设注入验证）
    r = c.post("/api/settings/models", json={
        "name": "DeepSeek 红灯验收",
        "protocol": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": API_KEY,
        "model_name": "deepseek-chat",
        "temperature": 0.2,
    })
    check("保存模型配置 201", r.status_code == 201, r.text[:80])
    t = c.post(f"/api/settings/models/{r.json()['id']}/test").json()
    check(f"连通测试 ok={t['ok']}", t["ok"], t.get("message", ""))

    # 2. 新建验收员工（带老师人设）
    for a in c.get("/api/assistants").json()["assistants"]:
        if a["name"] == "红灯验收员工":
            c.delete(f"/api/assistants/{a['id']}")
    r = c.post("/api/assistants", json={
        "name": "红灯验收员工", "employee_no": "RED001", "template_type": "standard",
        "teacher_persona": "陈老师，深耕技术面，风格权威笃定、平视交流（用「你」），从不承诺收益、不报具体点位。",
    })
    check("新建员工（带老师人设）", r.status_code == 201)
    aid = r.json()["id"]
    check("人设回读", r.json().get("teacher_persona", "").startswith("陈老师"))

    # 3. 红灯样例质检
    dialogue = """[客] 陈老师，我满仓了这只票，现在跌了不少，很慌。
[助] 你别慌，听我的：这只票明天肯定反弹，你等它跌到12块钱的时候直接重仓抄底，保准回本。
[客] 真的能回本吗？会不会继续跌？
[助] 肯定能回本，你就听我的加仓，别的不用管，别自己瞎操作。
[客] 好的老师，那我明天就加仓。
[助] 对，就这么办，跌到12块就买，稳赚。"""
    started = time.perf_counter()
    r = c.post(f"/api/assistants/{aid}/inspections",
               json={"session_title": "红灯验收：报点位+承诺收益", "raw_dialogue": dialogue})
    elapsed = int(time.perf_counter() - started)
    if r.status_code != 201:
        print(f"[FAIL] 质检失败: {r.text[:400]}")
        sys.exit(1)
    data = r.json()
    print(f"[INFO] 总分={data['total_score']} 红灯={data['is_red_alert']} 黄灯={data['is_yellow_alert']} 耗时{elapsed}s")
    print(f"[INFO] red_reasons={json.dumps(data['red_alert_reasons'], ensure_ascii=False)}")
    check("红灯一票否决已触发", data["is_red_alert"] is True, f"score={data['total_score']}")
    check("红灯 reasons 非空", bool(data["red_alert_reasons"]))

    # 4. 算术剥离 + 思维链：报告里的 S 维度分 == Σ子项；子项带 analysis
    rep = c.get(f"/api/reports/{data['id']}").json()
    s1 = rep["s_scores"]["s1_emotion_stabilize"]
    s1_sum = sum(v["score"] if isinstance(v, dict) else v for v in s1["sub_items"].values())
    check("S1 维度分 = Σ子项（后端汇总）", s1["score"] == s1_sum, f"s1={s1['score']} sub_sum={s1_sum}")
    d1 = rep["d_scores"]["d1_emotion_change"]
    check("D1 带 analysis 思维链", bool(d1.get("analysis")), d1.get("analysis", "")[:50])
    sub0 = next(iter(s1["sub_items"].values()))
    check("S 子项对象化且带 analysis", isinstance(sub0, dict) and "score" in sub0 and "analysis" in sub0)

    # 5. 老师腔改写验收：ai_rewrite 不得再报具体数字/承诺收益
    rewrites = [h["ai_rewrite"] for h in rep["highlight_dialogue"] if h.get("ai_rewrite")]
    print(f"[INFO] highlight={len(rep['highlight_dialogue'])}条 改写示例: {(rewrites[0] if rewrites else '')[:80]}")
    if rewrites:
        bad_tokens = ["12块", "12 块", "12块钱", "保准", "肯定回本", "稳赚", "保证"]
        # 排除批判性否定引用（如"市场没有稳赚"是合规教育话术，非承诺）
        leaked = [
            tok
            for tok in bad_tokens
            if any(
                tok in w and f"没有{tok}" not in w and f"不是{tok}" not in w and f"不要{tok}" not in w
                for w in rewrites
            )
        ]
        check("改写不含点位/承诺收益", not leaked, f"泄露: {leaked}")

    print(f"\n全部通过：{len(passed)} 项（真实 DeepSeek v2 提示词验收）")
    print(f"验收员工 id={aid}，报告页：http://127.0.0.1:8321/#/report/{data['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
