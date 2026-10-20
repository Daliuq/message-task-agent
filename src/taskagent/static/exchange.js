(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let preview = null;
  let exportAttempt = null;
  let refreshing = false;
  const report = value => { $("exchangeImportReport").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
  const status = text => { $("exchangeStatus").textContent = text; };
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify(body)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `本地服务返回 ${response.status}`);
    return result;
  }
  function invalidatePreview() { preview = null; $("exchangeApplyButton").disabled = true; }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [scopes, batches, candidates] = await Promise.all([
        request("/api/exchange/scopes"), request("/api/exchange/exports"), request("/api/exchange/candidates")
      ]);
      const selected = new Set(Array.from(document.querySelectorAll("[data-exchange-scope]:checked")).map(n => n.value));
      $("exchangeScopes").innerHTML = scopes.length ? scopes.map(s => `
        <label><input type="checkbox" data-exchange-scope value="${esc(s.id)}" ${selected.has(s.id) ? "checked" : ""}>
        <span>${esc(s.display_name)} · ${esc(s.scope_key)}<small>${esc(s.kind)} · ${Number(s.item_count)} 条</small></span></label>`).join("") :
        "<p>还没有本地来源。请先采集，或导入原始消息包。</p>";
      $("exchangeBatches").innerHTML = batches.length ? batches.map(b => {
        const base = `/api/exchange/exports/${encodeURIComponent(b.id)}`;
        const links = [
          ["analysis-request.json", "解析请求"], ["prompt.txt", "提示词文本"], ["prompt.json", "提示词 JSON"],
          ["messages.json", "原始消息"], ["analysis-result.template.json", "结果模板"],
          ["analysis-result.json", "已导入结果"]
        ].map(([name, label]) => `<a href="${base}/files/${name}" download="${name}">${label}</a>`).join("");
        const state = b.state === "cancelled" ? "已取消" : b.state === "preparing" ? "文件输出未完成" :
          b.all_parsed ? (b.review_event_count ? "解析齐全 · 业务待确认" : "解析齐全") : "等待结果";
        return `<article class="exchange-batch"><div class="exchange-batch-head"><strong>${esc(state)}</strong><small>${esc(b.created_at)}</small></div>
          <p>${Number(b.event_count)} 条消息 · 已解析 ${Number(b.parsed_count)} · 业务待确认 ${Number(b.review_event_count)} · 失败 ${Number(b.failed_count)} · 缺失 ${Number(b.missing_count)}</p>
          <code>${esc(b.id)}</code><div class="exchange-links">${links}</div>
          ${b.state === "preparing" ? `<button class="text-button" data-exchange-resume="${esc(b.id)}">恢复文件输出</button>` : ""}
          ${b.state !== "cancelled" && !b.all_parsed ? `<button class="text-button" data-exchange-cancel="${esc(b.id)}">取消并释放未处理消息</button>` : ""}
          </article>`;
      }).join("") : '<div class="empty-state"><strong>尚无导出批次</strong>创建后可下载请求包、提示词与结果模板。</div>';
      $("exchangeCandidates").innerHTML = candidates.length ? candidates.map(c => `
        <article class="exchange-batch"><strong>${esc(c.candidate.title)}</strong><p>${esc(c.candidate.description)}</p>
        <p>证据：${esc(c.candidate.evidence_quote)}</p><small>截止：${esc(c.candidate.due_at || "未提供")} · 状态：待确认</small></article>`).join("") :
        '<div class="empty-state"><strong>尚无解析候选</strong>导入有效解析结果后显示；不会自动当作已完成事项。</div>';
    } catch (error) { status(error.message); }
    finally { refreshing = false; }
  }
  document.addEventListener("workspace:view", event => { if (event.detail === "exchange") refresh(); });
  $("exchangeRefreshButton").addEventListener("click", refresh);
  $("refreshButton").addEventListener("click", () => { if (document.body.dataset.view === "exchange") refresh(); });
  $("exchangeMode").addEventListener("change", () => { $("exchangeRangeFields").hidden = $("exchangeMode").value !== "range"; });
  $("exchangeExportForm").addEventListener("submit", async event => {
    event.preventDefault();
    const scopes = Array.from(document.querySelectorAll("[data-exchange-scope]:checked")).map(n => n.value);
    if (!scopes.length) { status("请先选择至少一个本地来源范围。"); return; }
    const payload = { scope_ids: scopes, mode: $("exchangeMode").value, limit: Number($("exchangeLimit").value) };
    if (payload.mode === "range") {
      const start = $("exchangeStart").value, end = $("exchangeEnd").value;
      if (!start || !end || end < start) { status("请选择有效起止日期。"); return; }
      const followingDay = new Date(new Date(`${end}T00:00:00Z`).getTime() + 86400000).toISOString().slice(0, 10);
      payload.start = `${start}T00:00:00+08:00`;
      payload.end = `${followingDay}T00:00:00+08:00`;
    }
    const config = JSON.stringify(payload);
    if (!exportAttempt || exportAttempt.config !== config) exportAttempt = { config, key: crypto.randomUUID() };
    $("exchangeExportButton").disabled = true;
    status("正在冻结本地消息并输出文件…");
    try {
      const batch = await request("/api/exchange/exports", { ...payload, idempotency_key: exportAttempt.key });
      exportAttempt = null;
      status(`已输出 ${batch.event_count} 条消息。文件目录：${batch.directory}。未调用任何模型。`);
      await refresh();
    } catch (error) { status(error.message); await refresh(); }
    finally { $("exchangeExportButton").disabled = false; }
  });
  $("exchangeImportKind").addEventListener("change", invalidatePreview);
  $("exchangeImportText").addEventListener("input", invalidatePreview);
  $("exchangeImportFile").addEventListener("change", async () => {
    invalidatePreview();
    const file = $("exchangeImportFile").files[0];
    if (!file) return;
    if (file.size > 6 * 1024 * 1024) { report("文件超过 6 MiB，请拆成更小批次。"); return; }
    $("exchangeImportText").value = await file.text();
    report("文件已读取，尚未写入数据库。请先预检。");
  });
  $("exchangeImportForm").addEventListener("submit", async event => {
    event.preventDefault();
    invalidatePreview();
    const kind = $("exchangeImportKind").value;
    const document = $("exchangeImportText").value;
    $("exchangePreviewButton").disabled = true;
    try {
      const result = await request(`/api/exchange/${kind}/preview`, { document });
      // If the user edited the input during the request, do not enable a stale preview.
      if (document !== $("exchangeImportText").value || kind !== $("exchangeImportKind").value) return;
      preview = { id: result.preview_id, kind };
      report(result);
      $("exchangeApplyButton").disabled = false;
    } catch (error) { report(error.message); }
    finally { $("exchangePreviewButton").disabled = false; }
  });
  $("exchangeApplyButton").addEventListener("click", async () => {
    if (!preview) return;
    const selected = preview;
    $("exchangeApplyButton").disabled = true;
    try {
      const result = await request(`/api/exchange/${selected.kind}/apply`, { preview_id: selected.id });
      report(result);
      invalidatePreview();
      status("导入结果已落库。没有发送邮件，也没有把解析候选自动变成正式任务。");
      await refresh();
    } catch (error) { report(error.message); invalidatePreview(); }
  });
  $("exchangeBatches").addEventListener("click", async event => {
    const button = event.target.closest("[data-exchange-cancel], [data-exchange-resume]");
    if (!button) return;
    const cancel = Boolean(button.dataset.exchangeCancel);
    const id = button.dataset.exchangeCancel || button.dataset.exchangeResume;
    if (cancel && !window.confirm("取消此请求并释放尚未成功解析的消息？原文件保留，但此请求的迟到回复将不再应用。")) return;
    button.disabled = true;
    try {
      await request(`/api/exchange/exports/${encodeURIComponent(id)}/${cancel ? "cancel" : "resume"}`, {});
      status(cancel ? "已释放未处理消息，可用“所有待解析”重新打包。" : "文件输出已恢复。");
      await refresh();
    } catch (error) { status(error.message); button.disabled = false; }
  });
})();
