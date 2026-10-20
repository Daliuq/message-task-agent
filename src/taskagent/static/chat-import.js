(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  let scopes = [], timer = null, loading = false, seenCompleted = new Set();
  const label = value => ({queued:'等待运行',running:'正在导入',completed:'导入完成',partial:'部分失败 · 可恢复',failed:'失败',cancelled:'已取消'}[value] || value);
  const displayTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) + '（北京）' : '原始时间未知';
  function coverage(scope) {
    const shards = scope.manifest?.shards || [];
    const starts = shards.map(s=>s.min_time).filter(n=>n>0), ends=shards.map(s=>s.max_time).filter(n=>n>0);
    const unknown = shards.reduce((n,s)=>n+Number(s.unknown_time_count||0),0);
    return (starts.length ? `快照记录时间：${displayTime(Math.min(...starts)*1000)} 至 ${displayTime(Math.max(...ends)*1000)}` : '本范围暂无可读取的有效时间记录') + (unknown ? `；源库 ${unknown} 条时间未知记录（范围导入不包含）` : '');
  }
  async function api(path, payload) {
    const response = await fetch(path, payload === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify(payload)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `请求失败 ${response.status}`);
    return value;
  }
  function renderScopes() {
    const selected = new Set(Array.from(document.querySelectorAll('[data-import-scope]:checked')).map(n => n.value));
    const full = $('chatImportMode').value !== 'incremental';
    const visible = scopes.filter(s => s.platform === $('chatImportPlatform').value);
    $('chatImportScopes').innerHTML = visible.length ? visible.map(s => {
      const eligible = s.enabled && (!full || s.full_scan_enabled);
      return `<label class="chat-import-scope ${eligible ? '' : 'is-disabled'}"><input type="checkbox" data-import-scope value="${esc(s.id)}" ${eligible ? '' : 'disabled'} ${eligible && selected.has(s.id) ? 'checked' : ''}><span>${esc(s.display_name)} <small>${s.platform === 'wechat' ? '微信' : 'QQ'} · ${Number(s.item_count)} 条已保存${eligible ? '' : ' · 请先开启对应扫描选项'}</small></span></label>`;
    }).join('') : '<p class="panel-meta">还没有选择聊天范围。先从上方发现结果中选择会话，再编辑扫描选项。</p>';
  }
  function renderJobs(jobs) {
    $('chatImportJobs').innerHTML = jobs.length ? jobs.map(job => `<article class="chat-import-job"><div class="chat-import-job-head"><strong>${esc(label(job.status))}</strong><span class="panel-meta">${job.config.platform === 'qq' ? 'QQ' : '微信'} · ${esc(job.config.mode)} · ${esc(displayTime(job.created_at))}</span></div><p>本任务读取 ${Number(job.processed)} 条${job.config.platform === 'qq' ? `（核对 ${job.scopes.reduce((n,s)=>n+(s.manifest?.shards||[]).reduce((v,r)=>v+Number(r.scanned_count||0),0),0)} 条源记录）` : ''} · 新增 ${Number(job.items)} 条 · 保存版本 ${Number(job.versions)} 个</p>${job.scopes.map(s => `<div class="chat-import-scope-progress"><span>${esc(s.display_name)} · ${esc(label(s.status))}</span><progress max="${Math.max(Number(s.total),Number(s.processed),1)}" value="${Number(s.processed)}"></progress><small>本地快照 ${s.manifest ? Number(s.total) : '待读取'} 条 / 已保存 ${Number(s.processed)} 条</small><small>${esc(coverage(s))}</small>${s.error ? `<pre class="chat-import-error">${esc(s.error)}</pre>` : ''}</div>`).join('')}<div class="chat-import-job-actions">${job.resumable ? `<button class="button button-secondary" type="button" data-resume-import="${esc(job.id)}">从检查点继续</button>` : ''}${!['completed','cancelled'].includes(job.status) ? `<button class="button button-quiet" type="button" data-cancel-import="${esc(job.id)}">取消导入</button>` : ''}<code>${esc(job.id)}</code></div></article>`).join('') : '<p class="panel-meta">暂无正文导入记录。</p>';
    for (const job of jobs) {
      if (job.status === 'completed' && !seenCompleted.has(job.id)) {
        seenCompleted.add(job.id);
        document.dispatchEvent(new CustomEvent('chat:imported'));
      }
    }
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    clearTimeout(timer);
    try {
      const [savedScopes,jobs,messages,qq] = await Promise.all([api('/api/subscriptions'),api('/api/chat-imports'),api('/api/chat-messages?limit=50'),api('/api/qq-local-status')]);
      scopes = savedScopes;
      renderScopes(); renderJobs(jobs);
      $('qqLocalReadStatus').textContent = qq.note + (qq.accounts.length ? '。' + qq.accounts.map(a => `${a.account}：${a.state === 'ready' ? '密钥已验证 · 可读取' : '等待取钥'}`).join('；') : '。默认目录下未发现账号消息库。');
      $('chatImportedMessages').innerHTML = messages.length ? messages.map(m => `<details class="chat-imported-message"><summary><strong>${esc(m.display_name)}</strong><span>${esc(displayTime(m.metadata.source_time))} · ${esc(m.metadata.message_type)}</span><p>${esc(m.content_text.slice(0,120))}</p></summary><pre>${esc(m.content_text)}</pre><p class="panel-meta">发送者：${esc(m.metadata.from || '未知')} · ID：${esc(m.external_id)}</p><a href="/api/chat-messages/${encodeURIComponent(m.id)}/raw" download>下载原始消息字段 JSON</a></details>`).join('') : '<p class="panel-meta">尚无聊天正文。发现列表和勾选订阅不会自动导入消息。</p>';
    } catch(error) { $('chatImportFeedback').textContent = error.message; }
    finally {
      loading = false;
      if (document.body.dataset.view === 'subscriptions') timer = setTimeout(refresh,3000);
    }
  }
  document.addEventListener('workspace:view', event => {
    clearTimeout(timer);
    if (event.detail === 'subscriptions') refresh();
  });
  document.addEventListener('chat:subscriptions-changed',refresh);
  $('chatImportRefresh').addEventListener('click',refresh);
  $('refreshButton').addEventListener('click',()=>{if(document.body.dataset.view === 'subscriptions') refresh();});
  $('chatImportPlatform').addEventListener('change',renderScopes);
  $('chatImportMode').addEventListener('change',()=>{
    const range = $('chatImportMode').value === 'range';
    $('chatImportDates').hidden = !range;
    $('chatImportStart').required = $('chatImportEnd').required = range;
    renderScopes();
  });
  $('chatImportForm').addEventListener('submit',async event=>{
    event.preventDefault();
    const selected = Array.from(document.querySelectorAll('[data-import-scope]:checked')).map(n=>n.value);
    if(!selected.length){$('chatImportFeedback').textContent='请先选择要导入的已启用会话。';return;}
    if(!window.confirm(`将读取所选 ${selected.length} 个${$('chatImportPlatform').value === 'qq' ? 'QQ' : '微信'}聊天的消息正文并保存到本机数据库。不调用 LLM、不发送消息。开始吗？`)) return;
    const payload={platform:$('chatImportPlatform').value,mode:$('chatImportMode').value,scope_ids:selected};
    if(payload.mode==='range'){payload.start=$('chatImportStart').value;payload.end=$('chatImportEnd').value;}
    $('chatImportStartButton').disabled=true;
    try{
      const job=await api('/api/chat-imports',payload);
      $('chatImportFeedback').textContent=`导入任务已创建：${job.id}。后台分页执行，进度保存在数据库。`;
      await refresh();
    }catch(error){$('chatImportFeedback').textContent=error.message;}
    finally{$('chatImportStartButton').disabled=false;}
  });
  $('chatImportJobs').addEventListener('click',async event=>{
    const button=event.target.closest('[data-resume-import],[data-cancel-import]');
    if(!button)return;
    const resume=Boolean(button.dataset.resumeImport),id=button.dataset.resumeImport||button.dataset.cancelImport;
    button.disabled=true;
    try{await api(`/api/chat-imports/${encodeURIComponent(id)}/${resume?'resume':'cancel'}`,{});$('chatImportFeedback').textContent=resume?'已请求从已保存检查点继续。':'已请求取消；已保存消息保留。';await refresh();}
    catch(error){$('chatImportFeedback').textContent=error.message;button.disabled=false;}
  });
})();
