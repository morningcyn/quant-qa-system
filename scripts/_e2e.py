# E2E 走查脚本：对运行中的本地服务做完整 HTTP 流程验证（真实 LLM 调用需先配置 Key）
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8321"
ROOT = Path(__file__).resolve().parent.parent

passed = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        sys.exit(1)
    passed.append(name)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=60)

    # 清理历史测试数据
    resp = c.get("/api/assistants")
    for a in resp.json()["assistants"]:
        if a["name"].startswith("E2E"):
            c.delete(f"/api/assistants/{a['id']}")

    # 1. 健康检查与静态页面
    check("health", c.get("/api/health").json()["status"] == "ok")
    idx = c.get("/")
    check("index.html 正常", idx.status_code == 200 and "质检助手" in idx.text)
    vendor = c.get("/static/vendor/echarts.min.js")
    check("vendor echarts 可加载", vendor.status_code == 200 and len(vendor.content) > 900_000)

    # 2. 新建员工
    r = c.post("/api/assistants", json={"name": "E2E测试员工", "employee_no": "E2E001", "template_type": "standard"})
    check("新建员工 201", r.status_code == 201, str(r.json()))
    aid = r.json()["id"]
    r = c.post("/api/assistants", json={"name": "冲突", "employee_no": "E2E001"})
    check("工号冲突 409", r.status_code == 409 and r.json()["code"] == "employee_no_conflict")

    # 3. 解析预览（低分样例）
    low = (ROOT / "samples" / "sample_low.txt").read_text(encoding="utf-8")
    r = c.post("/api/parse/preview", json={"raw_text": low})
    data = r.json()
    check("解析预览", r.status_code == 200 and data["role_stats"]["total"] == 12, json.dumps(data["role_stats"], ensure_ascii=False))

    # 4. 无 Key 时触发质检 → 引导错误（先清空模型配置，保证前提成立；后续步骤会重新配置）
    for m in c.get("/api/settings/models").json()["models"]:
        c.delete(f"/api/settings/models/{m['id']}")
    r = c.post(f"/api/assistants/{aid}/inspections", json={"session_title": "E2E", "raw_dialogue": low})
    check("无Key质检返回引导错误", r.status_code == 400 and r.json()["code"] == "not_configured", r.json().get("message", ""))

    # 5. 乱码文本 → parse_failed
    r = c.post(f"/api/assistants/{aid}/inspections", json={"raw_dialogue": "完全没有角色标记的普通文本"})
    check("乱码文本 parse_failed", r.status_code == 400 and r.json()["code"] == "parse_failed")

    # 6. 空统计
    r = c.get(f"/api/assistants/{aid}/stats/trend?days=30")
    check("趋势补零 30 点", len(r.json()["points"]) == 30 and r.json()["total_count"] == 0)
    r = c.get(f"/api/assistants/{aid}/stats/top3?days=30")
    check("Top3 空数据", r.status_code == 200 and r.json()["dimensions"] == [])

    # 7. 设置接口
    r = c.get("/api/settings/models")
    check("模型列表为空", r.json()["models"] == [])
    r = c.post("/api/settings/models", json={
        "name": "E2E模型", "protocol": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-e2e-secret-123", "model_name": "deepseek-chat", "temperature": 0.2,
    })
    check("保存模型(密钥不回落)", r.status_code == 201 and "sk-e2e-secret-123" not in r.text)
    mid = r.json()["id"]
    r = c.get("/api/settings/models")
    check("密钥掩码", "sk-e2e-secret-123" not in r.text and r.json()["models"][0]["has_api_key"])
    r = c.post(f"/api/settings/models/{mid}/test")
    print(f"  [INFO] 连通测试(假Key): ok={r.json()['ok']} message={r.json()['message']}")
    check("连通测试返回结构", r.status_code == 200 and "ok" in r.json() and r.json()["ok"] is False)
    c.delete(f"/api/settings/models/{mid}")

    # 8. 模板接口
    r = c.get("/api/settings/templates")
    tpl = r.json()["templates"][0]["config"]
    check("三套模板就绪", len(r.json()["templates"]) == 3)
    bad = json.loads(json.dumps(tpl)); bad["d"]["d1"]["max"] = 9
    r = c.put("/api/settings/templates/standard", json={"name": "x", "config": bad})
    check("模板校验拦截", r.status_code == 400 and "D 端" in r.json()["message"])

    # 9. 清理
    c.delete(f"/api/assistants/{aid}")
    r = c.get("/api/assistants")
    check("清理测试数据", all(not a["name"].startswith("E2E") for a in r.json()["assistants"]))

    print(f"\n全部通过：{len(passed)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
