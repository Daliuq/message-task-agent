(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time = value => value ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) : '—';
  const labels = {completed:'运行已结束',failed:'运行失败',cancelled:'运行已取消',pending:'待解析',awaiting_result:'等待解析回复',succeeded:'已解析',processing:'解析中'};
  let messages = [], busy = false, timer;
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `本地服务返回 ${response.status}`);
    return result;
  }
  function render() {
    const query = $('scheduledSearch').value.trim().toLocaleLowerCase(), provider = $('scheduledProvider').value;
    const rows = messages.filter(r => (!provider || r.metadata.provider === provider) && (!query || [r.metadata.title,r.metadata.task_id,r.metadata.run_id,r.content_text].join(' ').toLocaleLowerCase().includes(query)));
    $('scheduledCount').textContent = `${rows.length} / ${messages.length} 条`;
    $('scheduledMessages').innerHTML = rows.length ? rows.map(r => {
      const m = r.metadata;
      return `<details class="scheduled-message"><summary><span class="scheduled-provider">${esc(m.provider === 'codex' ? 'Codex' : 'ChatGPT')}</span><strong>${esc(m.title)}</strong><time>${esc(time(m.source_time))} 北京时间</time><span class="scheduled-result-state">${esc(labels[m.run_status] || m.run_status)} · ${esc(m.completeness === 'full' ? (labels[r.event_status] || '解析记录待核对') : '仅通知摘要')}</span></summary><div class="scheduled-message-body"><p>任务 ID：<code>${esc(m.task_id)}</code><br>运行 ID：<code>${esc(m.run_id)}</code></p><pre>${esc(r.content_text)}</pre><a class="text-button" href="/api/scheduled/messages/${encodeURIComponent(r.id)}/raw" download="scheduled-result.json">下载源结果 ↗</a></div></details>`;
    }).join('') : '<div class="empty-state"><strong>还没有匹配的执行结果</strong>点击扫描，或导入真实的 ChatGPT 结果文件。</div>';
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const [status, rows] = await Promise.all([request('/api/scheduled/status'),request('/api/scheduled/messages?limit=100')]);
      messages = rows; render();
      $('scheduledInbox').textContent = status.chatgpt_inbox;
      const result = status.latest_result;
      const source = (result?.sources || []).find(s => s.provider === 'codex');
      const warnings = source?.warnings || [];
      $('scheduledSources').innerHTML = `<article><span class="eyebrow">本机 · 只读</span><h3>Codex 定时运行</h3><strong>${Number(status.counts.codex)} 条已保存</strong><p>${esc(source ? (source.error || `最近读取 ${source.read} 条 · 新增 ${source.items || 0} · 变化 ${Math.max(0,(source.versions || 0)-(source.items || 0))} · 无变化 ${source.unchanged || 0}`) : '扫描所有本机注册的 cron / 对话定时任务结果。')}</p>${warnings.length ? `<details><summary>${warnings.length} 条读取提示</summary><ul>${warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul></details>` : ''}</article><article class="scheduled-pending"><span class="eyebrow">网页 / 客户端 · 待连接</span><h3>ChatGPT 定时运行</h3><strong>${Number(status.counts.chatgpt)} 条已保存</strong><p>当前只扫描本地结果收件箱；尚未读取云端定时任务，不能确认云端是否有新增。</p></article>`;
      const job = status.latest_job;
      const running = job?.status === 'running';
      $('scheduledScan').disabled = running;
      if (running) {
        $('scheduledFeedback').textContent = '正在扫描本机全部定时结果与本地收件箱…';
        clearTimeout(timer); timer = setTimeout(refresh, 1500);
      } else if (result) {
        const inbox = result.sources.find(s => s.provider === 'chatgpt');
        $('scheduledFeedback').textContent = `${time(result.finished_at)} · 本机扫描结束；ChatGPT 云端未连接。${inbox?.warnings?.length ? '收件箱错误：' + inbox.warnings.join('；') : ''}`;
      } else $('scheduledFeedback').textContent = job?.status === 'interrupted' ? '上次扫描中断，可重新扫描；已保存的数据不会重复入库。' : '尚未扫描。只读采集，不修改原任务，不发送消息。';
    } catch (error) { $('scheduledFeedback').textContent = error.message; }
    finally { busy = false; }
  }
  $('scheduledScan').addEventListener('click', async () => {
    $('scheduledScan').disabled = true;
    try { await request('/api/scheduled/scan', {}); await refresh(); }
    catch (error) { $('scheduledFeedback').textContent = error.message; $('scheduledScan').disabled = false; }
  });
  $('scheduledRefresh').addEventListener('click', refresh);
  $('scheduledProvider').addEventListener('change', render);
  $('scheduledSearch').addEventListener('input', render);
  $('scheduledImportForm').addEventListener('submit', async event => {
    event.preventDefault(); $('scheduledImportButton').disabled = true;
    try {
      const file = $('scheduledImportFile').files[0];
      if (file?.size > 8 * 1024 * 1024) throw new Error('结果包最多 8 MiB');
      const text = file ? await file.text() : $('scheduledImportText').value;
      const r = await request('/api/scheduled/import', JSON.parse(text.replace(/^\uFEFF/, '')));
      await refresh();
      $('scheduledFeedback').textContent = `已保存：新增 ${r.items} 条，新增版本 ${r.versions} 个，无变化 ${r.unchanged} 条。未启动解析或发送消息。`;
    } catch (error) { $('scheduledFeedback').textContent = error.message; }
    finally { $('scheduledImportButton').disabled = false; }
  });
  document.addEventListener('workspace:view', e => { if (e.detail === 'scheduled') refresh(); });
  $('refreshButton').addEventListener('click', () => { if (document.body.dataset.view === 'scheduled') refresh(); });
})();
