// 上传对话框：粘贴 / 文件两 tab + 解析预览 + 提交质检（阶段动画 + 可取消）
(function () {
  "use strict";
  window.Views = window.Views || {};

  const STAGES = [
    "正在解析对话轮次…",
    "正在按 S/D 规则评分…",
    "正在定位失分话术…",
    "正在生成黄金改写与建议…",
  ];

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsText(file, "utf-8");
    });
  }

  function buildStageBox(onCancel) {
    const box = document.createElement("div");
    box.className = "stage-box";
    box.innerHTML = `
      <div class="stage-title">AI 质检进行中，请稍候…</div>
      <div class="stage-steps">
        ${STAGES.map((s) => `<div class="stage-step"><span class="dot"></span><span>${s}</span></div>`).join("")}
      </div>
      <div class="mt-16"><button class="btn btn-ghost" id="btn-cancel-run">取消</button></div>`;
    box.querySelector("#btn-cancel-run").addEventListener("click", () => {
      if (onCancel) onCancel();
    });
    return box;
  }

  function runStages(box) {
    const steps = box.querySelectorAll(".stage-step");
    let i = 0;
    const timer = setInterval(() => {
      if (i < steps.length) steps[i].classList.add("active");
      if (i > 0) steps[i - 1].classList.replace("active", "done");
      i++;
      if (i > steps.length) clearInterval(timer);
    }, 7000);
    return timer;
  }

  function buildForm(assistant) {
    const form = document.createElement("div");
    form.innerHTML = `
      <div class="form-row">
        <label class="form-label">会话标题（可选）</label>
        <input class="input" id="u-title" maxlength="200" placeholder="例如：2026-08-24 王先生 套牢解套咨询">
      </div>
      <div class="tabs" id="u-tabs">
        <span class="tab active" data-tab="paste">粘贴文本</span>
        <span class="tab" data-tab="file">上传文件</span>
      </div>
      <div id="u-pane-paste">
        <textarea class="textarea" id="u-text" placeholder="粘贴导出的聊天记录，支持格式：
[客] 客户说的话
[助] 助理说的话

也支持「客：… / 助：…」、CSV（角色,内容）、JSON（[{\"role\":\"客\",\"content\":\"…\"}]）"></textarea>
      </div>
      <div id="u-pane-file" style="display:none">
        <input type="file" id="u-file" accept=".txt,.csv,.json" style="color:var(--text-2)">
        <div class="form-hint">支持 TXT / CSV / JSON 导出文件（UTF-8 编码），文件内容读取后同样走格式解析</div>
      </div>
      <div id="u-preview" style="display:none" class="mt-16"></div>
      <div class="form-actions">
        <button class="btn btn-ghost" id="u-cancel">取消</button>
        <button class="btn btn-ghost" id="u-preview-btn">解析预览</button>
        <button class="btn btn-primary" id="u-submit" disabled>开始质检</button>
      </div>`;

    // tab 切换
    form.querySelectorAll("#u-tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        form.querySelectorAll("#u-tabs .tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        const isPaste = tab.dataset.tab === "paste";
        form.querySelector("#u-pane-paste").style.display = isPaste ? "" : "none";
        form.querySelector("#u-pane-file").style.display = isPaste ? "none" : "";
      });
    });
    form.querySelector("#u-file").addEventListener("change", async () => {
      const file = form.querySelector("#u-file").files[0];
      if (!file) return;
      try {
        const text = await readFile(file);
        form.querySelector("#u-text").value = text;
        UI.toast(`已读取文件：${file.name}（${text.length} 字）`, "success");
        clearPreview(form);
      } catch (err) {
        UI.handleError(err, "文件读取失败");
      }
    });

    function clearPreview() {
      const preview = form.querySelector("#u-preview");
      preview.style.display = "none";
      preview.innerHTML = "";
      form.querySelector("#u-submit").disabled = true;
    }

    async function doPreview() {
      const text = form.querySelector("#u-text").value.trim();
      if (!text) { UI.toast("请先粘贴或上传会话内容", "warning"); return; }
      const btn = form.querySelector("#u-preview-btn");
      btn.disabled = true;
      try {
        const data = await API.post("/api/parse/preview", { raw_text: text });
        const preview = form.querySelector("#u-preview");
        const speakers = data.speakers || [];
        // 本次评估对象（必选）：多角色对话只对指定助理计分，其余角色视为上下文背景
        const evaluateeRow = speakers.length
          ? `<div class="form-row mt-16" style="margin-bottom:0">
              <label class="form-label">本次评估对象 <span style="color:var(--critical)">*</span>（仅对该对象计分，其余角色为上下文背景）</label>
              <select class="select" id="u-evaluatee">
                ${speakers.map((s) => `<option value="${UI.esc(s)}">${UI.esc(s)}</option>`).join("")}
              </select>
            </div>`
          : "";
        preview.style.display = "";
        preview.innerHTML = `
          <div class="alert ${data.warnings.length ? "alert-warning" : "alert-info"} mb-8">
            <div>${data.warnings.length ? "⚠️" : "ℹ️"}</div>
            <div>${data.warnings.length ? data.warnings.map((w) => `<div>· ${UI.esc(w)}</div>`).join("") : "格式识别正常，可以开始质检"}
            <div style="margin-top:4px;color:var(--text-2)">共识别 ${data.role_stats.total} 轮（客户 ${data.role_stats["客"] || 0} 轮 / 助理 ${data.role_stats["助"] || 0} 轮）· 格式：${UI.esc(data.fmt)}</div></div>
          </div>
          ${evaluateeRow}
          <div class="mt-8" style="max-height:300px;overflow-y:auto">
            ${data.turns.slice(0, 50).map((t) => `
              <div class="turn-bubble ${t.role === "客" ? "customer" : "assistant"}">
                <span class="turn-no num">#${t.turn_no}</span>
                <div class="bubble">${UI.esc(t.speaker || "")}：${UI.esc(t.text.length > 200 ? t.text.slice(0, 200) + "…" : t.text)}</div>
              </div>`).join("")}
            ${data.turns.length > 50 ? `<div class="muted" style="text-align:center">… 仅预览前 50 轮</div>` : ""}
          </div>`;
        form.querySelector("#u-submit").disabled = false;
      } catch (err) {
        UI.handleError(err, "解析失败");
      } finally {
        btn.disabled = false;
      }
    }

    form.querySelector("#u-preview-btn").addEventListener("click", doPreview);
    form.querySelector("#u-text").addEventListener("input", clearPreview);

    return form;
  }

  async function doSubmit(assistant, form, modal, title) {
    const raw = form.querySelector("#u-text").value.trim();
    const evaluateeSel = form.querySelector("#u-evaluatee");
    const evaluatee = evaluateeSel ? evaluateeSel.value.trim() : "";
    if (!evaluatee) {
      UI.toast("请先解析预览并选择本次评估对象", "warning");
      return;
    }
    const bodyEl = modal.body;
    const controller = new AbortController();
    let stageTimer = null;

    // 阶段动画接管整个 body
    const stageBox = buildStageBox(() => {
      controller.abort();
      modal.close();
      UI.toast("已取消本次质检", "warning");
    });
    while (bodyEl.firstChild) bodyEl.removeChild(bodyEl.firstChild);
    bodyEl.appendChild(stageBox);
    stageTimer = runStages(stageBox);

    try {
      const resp = await fetch(`/api/assistants/${assistant.id}/inspections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_title: title || null, raw_dialogue: raw, evaluatee }),
        signal: controller.signal,
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        throw { code: (data && data.code) || "error", message: (data && data.message) || "质检失败" };
      }
      clearInterval(stageTimer);
      modal.close();
      UI.toast("质检完成，报告已归档", "success");
      location.hash = `#/report/${data.id}`;
    } catch (err) {
      clearInterval(stageTimer);
      if (err.name === "AbortError") return; // 用户取消
      modal.close();
      if (err.code === "not_configured") {
        UI.toast(err.message, "error");
        location.hash = "#/settings";
        return;
      }
      UI.handleError(err, "质检失败，请重试");
    }
  }

  Views.upload = {
    open(assistant) {
      const form = buildForm(assistant);
      const modal = UI.openModal({ title: `上传会话质检 — ${assistant.name}`, content: form, width: "lg" });
      form.querySelector("#u-cancel").addEventListener("click", modal.close);
      form.querySelector("#u-submit").addEventListener("click", () => {
        const title = form.querySelector("#u-title").value.trim();
        doSubmit(assistant, form, modal, title);
      });
    },
  };
})();
