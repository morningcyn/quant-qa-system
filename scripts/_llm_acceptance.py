# 真实 LLM 验收：配置模型 → 连通测试 → 用 3 条样例跑完整质检流水线 → 断言结果
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8321"
ROOT = Path(__file__).resolve().parent.parent
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

    # 0. 清理历史验收模型配置（保证本次新建即默认）
    for m in c.get("/api/settings/models").json()["models"]:
        if m["name"] == "DeepSeek 验收":
            c.delete(f"/api/settings/models/{m['id']}")

    # 1. 配置模型（DPAPI 加密落库）
    r = c.post("/api/settings/models", json={
        "name": "DeepSeek 验收",
        "protocol": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": API_KEY,
        "model_name": "deepseek-chat",
        "temperature": 0.2,
    })
    check("保存模型配置 201", r.status_code == 201, r.text[:80])
    model_id = r.json()["id"]
    r = c.get("/api/settings/models")
    check("密钥掩码不回落", API_KEY not in r.text and r.json()["models"][0]["has_api_key"])
    check("自动设为默认", str(r.json()["active_model_id"]) == model_id)

    # 2. 连通测试
    r = c.post(f"/api/settings/models/{model_id}/test")
    t = r.json()
    print(f"  [INFO] 连通测试: ok={t['ok']} latency={t.get('latency_ms')}ms message={t['message']}")
    check("连通测试成功", t["ok"])

    # 3. 清理旧验收员工
    for a in c.get("/api/assistants").json()["assistants"]:
        if a["name"] == "LLM验收员工":
            c.delete(f"/api/assistants/{a['id']}")

    r = c.post("/api/assistants", json={"name": "LLM验收员工", "employee_no": "LLM001", "template_type": "standard"})
    aid = r.json()["id"]

    # 4. 三条样例真实质检（每条耗时 20~60s）
    # 验收准则：高分样例应明显高分且非黄灯；低分样例应 <59 且黄灯；所有样例「分数<59 ↔ 黄灯」自洽（后端熔断重算保证）
    samples = [
        ("sample_high.txt", "高分样例", "good"),
        ("sample_mid.txt", "中分样例", "mid"),
        ("sample_low.txt", "低分样例", "low"),
    ]
    results = []
    for fname, label, kind in samples:
        text = (ROOT / "samples" / fname).read_text(encoding="utf-8")
        started = time.perf_counter()
        r = c.post(f"/api/assistants/{aid}/inspections",
                   json={"session_title": f"{label}（真实验收）", "raw_dialogue": text})
        elapsed = int(time.perf_counter() - started)
        if r.status_code != 201:
            print(f"[FAIL] {label} 质检失败: {r.text[:300]}")
            sys.exit(1)
        data = r.json()
        score, yellow = data["total_score"], data["is_yellow_alert"]
        coherent = (score < 59) == yellow  # 熔断自洽
        if kind == "good":
            good_ok = score >= 75 and not yellow
        elif kind == "low":
            good_ok = score < 59 and yellow
        else:
            good_ok = True  # 中分样例只要求自洽，分数高低反映 DeepSeek 严格度
        check(
            f"{label} 质检成功 score={score} 黄灯={yellow} 自洽={coherent} 耗时{elapsed}s",
            coherent and good_ok,
        )
        results.append(data)
        print(f"  [INFO] 维度分: D1={data['d_scores']['d1_emotion_change']['score']} D2={data['d_scores']['d2_profile_match']['score']} "
              f"D3={data['d_scores']['d3_problem_match']['score']} D4={data['d_scores']['d4_expectation_exceed']['score']} | "
              f"S1={data['s_scores']['s1_emotion_stabilize']['score']} S2={data['s_scores']['s2_problem_closure']['score']} S3={data['s_scores']['s3_professional_supply']['score']}")
        print(f"  [INFO] highlight={len(data['highlight_dialogue'])}条 建议={len(data['improvement_suggestions'])}条 画像={data['customer_profile']}")

    # 5. 深度检查：highlight 轮次对齐、改写非空、黄灯 reasons 非空
    for label, data in zip(("高分", "中分", "低分"), results):
        turns_ok = all(1 <= h["turn"] <= data["turn_count"] for h in data["highlight_dialogue"])
        rewrite_ok = all(h["ai_rewrite"] and len(h["ai_rewrite"]) > 10 for h in data["highlight_dialogue"])
        check(f"{label}样例 highlight 轮次对齐且改写有效", turns_ok and rewrite_ok)
    check("低分样例黄灯 reasons 非空", results[2]["yellow_alert_reasons"] != [],
          json.dumps(results[2]["yellow_alert_reasons"], ensure_ascii=False))

    # 6. 统计聚合（真实数据）
    trend = c.get(f"/api/assistants/{aid}/stats/trend?days=30").json()
    check("真实数据趋势统计", trend["total_count"] == 3 and trend["latest_score"] is not None,
          f"latest={trend['latest_score']} yellow={trend['yellow_count']}")
    top3 = c.get(f"/api/assistants/{aid}/stats/top3?days=30").json()
    check("真实数据 Top3 聚合", len(top3["dimensions"]) == 3 and len(top3["sub_items"]) == 3,
          json.dumps(top3["dimensions"], ensure_ascii=False))

    print(f"\n全部通过：{len(passed)} 项（真实 DeepSeek 调用）")
    print(f"验收员工 id={aid}，报告页：http://127.0.0.1:8321/#/assistant/{aid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
