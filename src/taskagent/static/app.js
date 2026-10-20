(() => {
  const titles = {
    overview: ["总览", "本地工作台", "让消息有序。", "邮件增量、聊天订阅范围、待办与处理记录，集中在一个本地工作台。"],
    mail: ["邮件收件箱", "已保存的邮件", "邮件，按时间线归档。", "按收件时间查阅已持久化邮件；范围补扫与日常增量互不影响。"],
    subscriptions: ["聊天订阅", "扫描范围", "先扫描发现，再选择。", "由程序发现本机可用会话；你只需勾选需要管理的聊天。"],
    scheduled: ["定时任务结果", "结果采集", "让定时结果，也有来处。", "ChatGPT 与 Codex 的运行结果独立入库；保留原文、源时间和解析记录，不把运行结束当成待办完成。"],
    tasks: ["任务清单", "任务管理", "把重要的事往前推进。", "按截止时间和当前状态查看已生成任务。"],
    activity: ["采集与处理", "运行记录", "每一步都有记录。", "核对扫描进度、增量事件与 Agent 消费状态。"],
    exchange: ["数据交换", "网页聊天协作", "让解析往返，有据可查。", "导出增量与提示词，导入结构化回复；原始消息、解析记录和任务分开保存。"],
    outbox: ["邮件通知", "通知中心", "发送前先看清内容。", "通知只在本地预览；不会从此页面自动发送。"]
  };
  const state = { view: "overview", overview: null, mail: [], subscriptions: [], chatDiscovery: { wechat: { run: null, candidates: [] }, qq: { run: null, candidates: [] } }, tasks: [], events: [], scans: [], outbox: [], taskFilter: "all", search: "", mailCarouselOffset: 0, subscriptionEditingId: null };
  state.chatTags = [];
  state.chatAdded = new Set();
  state.chatRemoved = new Set();
  state.chatChecks = { available: new Set(), selected: new Set() };
  state.chatCatalog = new Map();
  state.chatTransferSaving = false;
  state.subscriptionTagFilter = "";
  let subscriptionTagDraft = [];
  let viewMotion = null;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const date = value => {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(d).replace("/", "-");
  };
  const shortDate = value => {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).format(d).replace("/", "-");
  };
  const rangeLabel = (start, end) => {
    const first = String(start || ""), last = String(end || "");
    if (first.endsWith("T00:00:00+08:00") && last.endsWith("T00:00:00+08:00")) {
      const [year, month, day] = last.slice(0, 10).split("-").map(Number);
      const inclusiveEnd = new Date(Date.UTC(year, month - 1, day) - 86400000).toISOString().slice(0, 10);
      return `${first.slice(0, 10)} 至 ${inclusiveEnd}（北京时间，含）`;
    }
    return `[${first}, ${last})`;
  };
  const statusName = value => ({ ok: "已完成", pending: "待处理", awaiting_result: "等待解析结果", processing: "处理中", succeeded: "已解析", failed: "失败", open: "待处理", completed: "已完成", deferred: "已延期", cancelled: "已取消", queued: "待发送", sent: "已接受", delivery_unknown: "结果未知" }[value] || value || "未消费");
  const badge = status => `<span class="state-badge ${esc(status || "pending")}">${esc(statusName(status || "pending"))}</span>`;
  const empty = (headline, subline) => `<div class="empty-state"><strong>${esc(headline)}</strong>${esc(subline)}</div>`;
  const excerpt = text => String(text || "").replace(/\s+/g, " ").trim();
  const toast = message => {
    const node = $("#toast"); node.textContent = message; node.classList.add("show");
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
  };
  async function getJSON(path) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) throw new Error(`本地服务返回 ${response.status}`);
    return response.json();
  }
  async function postJSON(path, payload) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" }, body: JSON.stringify(payload || {}) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `本地服务返回 ${response.status}`);
    return body;
  }
  async function refreshData(quiet = false) {
    try {
      const [overview, mail, subscriptions, chatDiscovery, tasks, events, scans, outbox, chatTags] = await Promise.all([
        getJSON("/api/overview"), getJSON("/api/mail?limit=200"), getJSON("/api/subscriptions"),
        getJSON("/api/chat-discovery"),
        getJSON("/api/tasks"), getJSON("/api/events?limit=100"), getJSON("/api/scans?limit=40"),
        getJSON("/api/outbox?limit=100"), getJSON("/api/chat-tags")
      ]);
      Object.assign(state, { overview, mail, subscriptions, chatDiscovery, tasks, events, scans, outbox, chatTags });
      $("#syncDot").classList.remove("offline");
      $("#syncLabel").textContent = `已连接 · ${new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date())} 更新`;
      $("#mailCount").textContent = mail.length;
      $("#taskCount").textContent = tasks.filter(item => item.status === "open").length;
      renderCurrent();
      initViewMotion();
      if (!quiet) toast("已从本地数据库刷新");
    } catch (error) {
      $("#syncDot").classList.add("offline");
      $("#syncLabel").textContent = "本地服务暂不可用";
      if (!quiet) toast(error.message || "连接失败");
    }
  }
  function setView(view) {
    state.view = view;
    $("#scanButton").hidden = view === "scheduled";
    document.body.dataset.view = view;
    window.scrollTo(0, 0);
    $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.view === view));
    $$(".view-panel").forEach(node => node.classList.toggle("active", node.id === `view-${view}`));
    const [crumb, eyebrow, title, description] = titles[view] || titles.overview;
    $("#crumbTitle").textContent = crumb;
    $("#pageEyebrow").textContent = eyebrow;
    const titleElement = $("#pageTitle");
    titleElement.textContent = title;
    $("#pageDescription").textContent = description;
    if (view === "overview") {
      const inlineImage = document.createElement("span");
      const image = document.createElement("img");
      inlineImage.className = "inline-title-image";
      inlineImage.setAttribute("aria-hidden", "true");
      image.src = "/art/workflow.svg";
      image.alt = "";
      inlineImage.appendChild(image);
      titleElement.replaceChildren(document.createTextNode("让消息"), inlineImage,
        document.createTextNode("有序。"));
    }
    $("#searchInput").value = "";
    state.search = "";
    renderCurrent();
    initViewMotion();
    document.dispatchEvent(new CustomEvent("workspace:view", { detail: view }));
  }
  function renderCurrent() {
    if (!state.overview) return;
    if (state.view === "overview") renderOverview();
    if (state.view === "mail") renderMail();
    if (state.view === "subscriptions") renderSubscriptions();
    if (state.view === "tasks") renderTasks();
    if (state.view === "activity") renderActivity();
    if (state.view === "outbox") renderOutbox();
  }
  function renderOverview() {
    const o = state.overview, c = o.counts || {}, events = o.event_status || {};
    const metrics = [
      ["消息记录", c.source_items || 0, `邮件 ${c.mail_items || 0} · 聊天 ${c.chat_items || 0} · 定时 ${c.scheduled_items || 0}`, "↘"],
      ["待消费事件", c.pending_events || 0, "等待 Agent 处理", "◷"],
      ["待办任务", o.open_tasks || 0, `共 ${c.tasks || 0} 条任务`, "✓"],
      ["通知记录", c.notification_outbox || 0, "发送需单独触发", "↗"]
    ];
    $("#metricGrid").innerHTML = metrics.map(([label, value, detail, mark]) => `<article class="metric-card"><div class="metric-top"><span>${esc(label)}</span><span class="metric-mark">${esc(mark)}</span></div><div class="metric-number">${Number(value).toLocaleString("zh-CN")}</div><div class="metric-bottom"><span>${esc(detail)}</span><strong>${label === "待消费事件" && value ? "待处理" : "本地"}</strong></div></article>`).join("");
    const pageSize = 2;
    const lastOffset = Math.max(0, state.mail.length - pageSize);
    state.mailCarouselOffset = Math.min(state.mailCarouselOffset, lastOffset);
    const recent = state.mail.slice(state.mailCarouselOffset, state.mailCarouselOffset + pageSize);
    $("#mailCarouselCount").textContent = state.mail.length
      ? `${state.mailCarouselOffset + 1}–${Math.min(state.mailCarouselOffset + recent.length, state.mail.length)} / ${state.mail.length}`
      : "0 封";
    $$('[data-mail-carousel="prev"]').forEach(button => { button.disabled = state.mailCarouselOffset <= 0; });
    $$('[data-mail-carousel="next"]').forEach(button => { button.disabled = state.mailCarouselOffset >= lastOffset; });
    $("#recentMail").innerHTML = recent.length ? recent.map(item => {
      const m = item.metadata || {};
      return `<div class="mail-preview" data-mail-id="${esc(item.item_id)}"><span class="mail-avatar">${esc((m.from || "邮").trim().slice(0, 1).toUpperCase())}</span><div class="mail-preview-main"><div class="mail-preview-top"><span class="mail-subject">${esc(m.subject || "(无主题)")}</span><span class="mail-date">${esc(shortDate(m.date || item.observed_at))}</span></div><div class="mail-sender">${esc(m.from || item.folder || "邮件")}</div></div>${badge(item.event_status)}</div>`;
    }).join("") : empty("还没有邮件", "执行一次增量扫描后，新邮件会显示在这里。");
    const p = Number(c.source_items || 0), pending = Number(c.pending_events || 0), done = Number(events.succeeded || 0), taskCount = Number(c.tasks || 0);
    const steps = [["消息采集", `${p} 条消息已保存`, p > 0, p], ["事件队列", pending ? `${pending} 条等待消费` : "没有待消费事件", pending === 0 && p > 0, pending], ["任务归并", `${taskCount} 条任务记录`, taskCount > 0, taskCount], ["处理完成", `${done} 条事件已消费`, done > 0, done]];
    $("#pipeline").innerHTML = steps.map(([name, detail, complete, count], i) => `<div class="pipeline-step ${complete ? "done" : ""}"><span class="step-node">${complete ? "✓" : i + 1}</span><div><div class="step-title">${esc(name)}</div><div class="step-detail">${esc(detail)}</div></div><span class="step-count">${Number(count).toString().padStart(2, "0")}</span></div>`).join("");
    $("#pipelineHint").textContent = pending ? `${pending} 条消息事件尚未被 Agent 消费；当前 UI 不会自动启动 Agent。` : (p && !taskCount ? "邮件已经落库；尚无任务生成记录。Agent 消费需要单独配置和启动。" : "没有待处理事件。邮箱扫描与任务消费分别记录。");
    const folders = o.folders || [];
    $("#folderTotal").textContent = `${folders.length} 个文件夹`;
    $("#folderGrid").innerHTML = folders.length ? folders.map(folder => `<div class="folder-card"><div class="folder-top"><span class="folder-name" title="${esc(folder.name)}">${esc(folder.name)}</span><i class="folder-status"></i></div><div class="folder-detail"><span>UID ${esc(folder.cursor?.uid ?? "—")}</span><span>${esc(date(folder.updated_at))}</span></div></div>`).join("") : empty("尚未建立游标", "扫描邮箱后会显示文件夹状态。");
  }
  function filteredMail() {
    const query = state.search.trim().toLowerCase();
    if (!query) return state.mail;
    return state.mail.filter(item => [item.metadata?.subject, item.metadata?.from, item.content_text, item.folder].join(" ").toLowerCase().includes(query));
  }
  function renderMail() {
    const list = filteredMail();
    $("#mailSummary").textContent = `邮件 · ${list.length}`;
    $("#mailList").innerHTML = list.length ? list.map(item => {
      const m = item.metadata || {};
      return `<article class="mail-row" data-mail-id="${esc(item.item_id)}"><span class="mail-avatar">${esc((m.from || "邮").trim().slice(0, 1).toUpperCase())}</span><div class="mail-row-main"><div class="mail-row-title"><strong>${esc(m.subject || "(无主题)")}</strong>${badge(item.event_status)}</div><div class="mail-row-meta">${esc(m.from || "未知发件人")} · ${esc(item.folder || "邮件")}</div><div class="mail-excerpt">${esc(excerpt(item.content_text).slice(0, 260) || "此邮件没有可预览的正文。")}</div></div><div class="mail-row-side"><span class="mail-row-date">${esc(date(m.date || item.observed_at))}</span><span class="panel-meta">${(m.attachments || []).length} 个附件</span></div></article>`;
    }).join("") : empty(state.mail.length ? "没有匹配的邮件" : "收件箱还是空的", state.mail.length ? "试试其他搜索词。" : "扫描邮箱后，新邮件和正文摘要会保存在这里。");
  }
  function renderSubscriptions() {
    const query = state.search.trim().toLowerCase();
    const all = state.subscriptions;
    const filter = $("#subscriptionTagFilter");
    filter.innerHTML = '<option value="">全部标签</option><option value="__untagged__">未分类</option>' + state.chatTags.map(tag => `<option value="${esc(tag.id)}">${esc(tag.name)} (${Number(tag.chat_count)})</option>`).join("");
    if (state.subscriptionTagFilter && state.subscriptionTagFilter !== "__untagged__" && !state.chatTags.some(t => t.id === state.subscriptionTagFilter)) state.subscriptionTagFilter = "";
    filter.value = state.subscriptionTagFilter;
    const list = all.filter(item => {
      const tags = item.tags || [];
      const matchTag = !state.subscriptionTagFilter || (state.subscriptionTagFilter === "__untagged__" ? !tags.length : tags.some(t => t.id === state.subscriptionTagFilter));
      return matchTag && (!query || [item.platform, item.account_key, item.account_name,
        item.conversation_key, item.display_name, ...tags.map(t => t.name)].join(" ").toLowerCase().includes(query));
    });
    $("#subscriptionCount").textContent = `${list.length} / ${all.length} 个会话`;
    $("#subscriptionList").innerHTML = list.length ? list.map(item => `<article class="subscription-card">
      <div class="subscription-main"><div class="subscription-title"><strong>${esc(item.display_name)}</strong><span class="platform-chip ${esc(item.platform)}">${item.platform === "wechat" ? "微信" : "QQ"}</span><span class="panel-meta">${Number(item.item_count || 0)} 条已采集记录</span></div>
      <div class="subscription-meta">账号：${esc(item.account_key)}${item.account_name && item.account_name !== item.account_key ? ` · ${esc(item.account_name)}` : ""}</div>
      <div class="subscription-meta">会话标识由扫描器维护 · 更新于 ${esc(date(item.updated_at))}</div><div class="chat-tag-badges">${(item.tags || []).length ? item.tags.map(tag => `<span class="chat-tag-badge">${esc(tag.name)}</span>`).join("") : '<span class="panel-meta">未分类</span>'}</div></div>
      <div class="subscription-card-actions"><div class="subscription-statuses"><span class="subscription-state ${item.enabled ? "is-on" : ""}">${item.enabled ? "订阅已启用" : "订阅未启用"}</span><span class="subscription-state ${item.full_scan_enabled ? "is-on" : ""}">${item.full_scan_enabled ? "参与全量扫描" : "未纳入全量扫描"}</span></div><button class="button button-secondary" type="button" data-config-subscription="${esc(item.id)}">编辑配置</button></div>
      </article>`).join("") : empty(all.length ? "没有匹配的聊天范围" : "还没有选择聊天范围", all.length ? "试试其他搜索词。" : "先扫描会话，再从发现结果中勾选需要管理的聊天。");
    renderChatDiscovery();
  }
  function renderChatDiscovery() {
    const platforms = ["wechat", "qq"];
    for (const platform of platforms) {
      const discovery = state.chatDiscovery?.[platform] || { run: null, candidates: [] };
      const run = discovery.run;
      const node = $(`#${platform}DiscoveryStatus`);
      if (node) {
        if (!run) node.textContent = "尚未扫描";
        else if (run.status === "completed") node.textContent = `发现 ${run.item_count} 个会话 · ${date(run.finished_at)}`;
        else if (run.status === "partial") node.textContent = `部分发现 ${run.item_count} 个会话 · ${run.error || date(run.finished_at)}`;
        else if (run.status === "failed") node.textContent = `扫描失败 · ${run.error || "请重试"}`;
        else node.textContent = "扫描中…";
      }
    }
    const keyFor = item => JSON.stringify([item.platform,item.account_key.toLowerCase(),item.conversation_key]);
    const catalog = new Map(state.subscriptions.map(item => [keyFor(item),{...item,scope_id:item.id,
      conversation_type:item.conversation_key.startsWith('group:') || item.conversation_key.endsWith('@chatroom') ? 'group':'private',
      message_count:item.item_count}]));
    for(const platform of platforms) for(const item of state.chatDiscovery?.[platform]?.candidates || []) {
      const key=keyFor({...item,platform}), existing=catalog.get(key);
      catalog.set(key,{...existing,...item,platform,scope_id:existing?.scope_id,candidate_id:item.id,tags:existing?.tags || []});
    }
    state.chatCatalog=catalog;
    const baseline=new Set([...catalog].filter(([,item])=>item.enabled).map(([key])=>key));
    state.chatAdded=new Set([...state.chatAdded].filter(key=>catalog.has(key) && !baseline.has(key)));
    state.chatRemoved=new Set([...state.chatRemoved].filter(key=>baseline.has(key)));
    const chosen=new Set([...baseline,...state.chatAdded].filter(key=>!state.chatRemoved.has(key)));
    const platformFilter=$("#chatDiscoveryPlatform").value, typeFilter=$("#chatDiscoveryType").value;
    const matching=(item,query)=>(!platformFilter || item.platform===platformFilter) && (!typeFilter || item.conversation_type===typeFilter) &&
      (!query || [item.display_name,item.account_key,item.conversation_key,...(item.tags||[]).map(t=>t.name)].join(' ').toLowerCase().includes(query));
    for(const side of ['available','selected']) {
      const selected=side==='selected', query=$(selected?'#chatSelectedSearch':'#chatDiscoverySearch').value.trim().toLowerCase();
      const all=[...catalog].filter(([key])=>chosen.has(key)===selected);
      const visible=all.filter(([,item])=>matching(item,query)).sort((a,b)=>a[1].display_name.localeCompare(b[1].display_name,'zh-CN'));
      state.chatChecks[side]=new Set([...state.chatChecks[side]].filter(key=>catalog.has(key) && chosen.has(key)===selected));
      const checks=state.chatChecks[side], count=selected?'#transferSelectedCount':'#transferAvailableCount';
      $(count).textContent=`${visible.length} / ${all.length} 个`;
      $(selected?'#chatSelectedList':'#chatCandidateList').innerHTML=visible.length ? visible.map(([key,item])=>{
        const pending=state.chatAdded.has(key)?'待新增':state.chatRemoved.has(key)?'待停用':selected?'已订阅':item.scope_id?'已停用':'';
        return `<label class="transfer-row ${checks.has(key)?'is-checked':''}"><input type="checkbox" data-transfer-side="${side}" data-transfer-key="${esc(key)}" aria-label="${selected?'移出':'加入'} ${esc(item.display_name)}" ${checks.has(key)?'checked':''} ${state.chatTransferSaving?'disabled':''}><span class="transfer-avatar ${esc(item.platform)}" aria-hidden="true">${item.conversation_type==='group'?'群':'私'}</span><span class="transfer-row-main"><strong title="${esc(item.display_name)}">${esc(item.display_name)}</strong><span><b>${item.platform==='qq'?'QQ':'微信'}</b> · ${item.conversation_type==='group'?'群聊':'私聊'} · ${Number(item.message_count||0).toLocaleString('zh-CN')} 条本地记录</span><small>账号：${esc(item.account_key)} · ${item.conversation_type==='group'?'群号':'会话'}：${esc(item.conversation_key.replace(/^group:/,''))}${(item.tags||[]).length?' · '+esc(item.tags.map(t=>t.name).join(' / ')):''}</small></span>${pending?`<em class="${pending.startsWith('待')?'is-pending':''}">${pending}</em>`:''}</label>`;
      }).join('') : empty(selected?'尚无匹配订阅':'没有匹配的可订阅聊天', selected?'从左侧勾选聊天并加入；也可调整搜索条件。':'调整搜索、平台或类型，或点击上方扫描会话。');
      const allInput=$(selected?'#transferAllRight':'#transferAllLeft');
      allInput.disabled=state.chatTransferSaving || !visible.length;
      allInput.checked=Boolean(visible.length && visible.every(([key])=>checks.has(key)));
      allInput.indeterminate=!allInput.checked && visible.some(([key])=>checks.has(key));
      allInput.dataset.keys=JSON.stringify(visible.map(([key])=>key));
      $(selected?'#transferRightChecked':'#transferLeftChecked').textContent=checks.size?`已勾选 ${checks.size}`:'';
    }
    $("#chatDiscoveryCount").textContent=`共 ${catalog.size} 个聊天 · 已选 ${chosen.size} 个`;
    updateCandidateSelectionUi();
  }
  function updateCandidateSelectionUi() {
    const added=state.chatAdded.size, removed=state.chatRemoved.size;
    $("#selectedChatCount").textContent=added || removed ? `待保存：新增 ${added} 个 · 停用 ${removed} 个`:'没有待保存变更';
    $("#saveChatSelections").disabled=state.chatTransferSaving || !(added || removed);
    $("#resetChatSelections").disabled=state.chatTransferSaving || !(added || removed);
    $("#transferAdd").disabled=state.chatTransferSaving || !state.chatChecks.available.size;
    $("#transferRemove").disabled=state.chatTransferSaving || !state.chatChecks.selected.size;
    $("#transferAdd").textContent='加入订阅 →'+(state.chatChecks.available.size?` (${state.chatChecks.available.size})`:'');
    $("#transferRemove").textContent='← 移出订阅'+(state.chatChecks.selected.size?` (${state.chatChecks.selected.size})`:'');
  }
  function moveChatSelections(add) {
    if(state.chatTransferSaving)return;
    const side=add?'available':'selected';
    for(const key of state.chatChecks[side]) {
      const item=state.chatCatalog.get(key); if(!item)continue;
      if(add){state.chatRemoved.delete(key);if(!item.enabled)state.chatAdded.add(key);}
      else{state.chatAdded.delete(key);if(item.enabled)state.chatRemoved.add(key);}
    }
    state.chatChecks[side].clear();
    renderChatDiscovery();
  }
  function renderTasks() {
    const query = state.search.trim().toLowerCase();
    const list = state.tasks.filter(task => (state.taskFilter === "all" || (state.taskFilter === "open" ? task.status === "open" : task.status === "completed")) && (!query || [task.title, task.description, task.identity_key].join(" ").toLowerCase().includes(query)));
    $("#taskList").innerHTML = list.length ? list.map(task => `<article class="task-card ${esc(task.status)}"><button class="task-check" data-complete="${esc(task.id)}" aria-label="标记完成"></button><div class="task-main"><div class="task-title-line"><span class="task-title">${esc(task.title)}</span>${badge(task.status)}<span class="priority p${esc(task.priority)}">优先级 ${esc(task.priority)}</span></div>${task.description ? `<div class="task-description">${esc(task.description)}</div>` : ""}<div class="task-meta"><span>截止 <strong>${esc(task.due_at ? date(task.due_at) : "未设置")}</strong></span><span>预计 <strong>${esc(task.estimated_minutes ? `${task.estimated_minutes} 分钟` : "未设置")}</strong></span><span>出现 <strong>${esc(task.occurrence_count || 0)} 次</strong></span></div></div><div class="task-actions"><select data-task-status="${esc(task.id)}" aria-label="更改任务状态"><option value="open" ${task.status === "open" ? "selected" : ""}>待处理</option><option value="completed" ${task.status === "completed" ? "selected" : ""}>已完成</option><option value="deferred" ${task.status === "deferred" ? "selected" : ""}>已延期</option><option value="cancelled" ${task.status === "cancelled" ? "selected" : ""}>已取消</option></select></div></article>`).join("") : empty(state.tasks.length ? "没有匹配的任务" : "还没有生成任务", state.tasks.length ? "调整筛选条件或搜索词。" : "新邮件先进入事件队列；任务需由配置的 Agent 独立消费后生成。");
  }
  function renderActivity() {
    $("#scanList").innerHTML = state.scans.length ? state.scans.map(item => {
      const range = item.cursor_before?.mode === "time_range_full";
      const chat = Boolean(item.cursor_before?.chat_job_id);
      const rangeText = chat ? `${item.item_count} 条 · 导入任务 ${item.cursor_before.chat_job_id}` : range ? rangeLabel(item.cursor_before.start_inclusive, item.cursor_before.end_exclusive) : `${item.item_count} 封`;
      return `<div class="activity-row"><span class="activity-dot ${item.status === "failed" ? "fail" : item.status === "baseline" ? "warn" : ""}"></span><div><div class="activity-title">${chat ? ({full:"聊天全量导入 · ",range:"聊天范围导入 · ",incremental:"聊天增量导入 · "}[item.cursor_before.mode] || "聊天导入 · ") : range ? "时间范围全量扫描 · " : "增量扫描 · "}${esc(item.scope_key)} · ${esc(statusName(item.status))}</div><div class="activity-sub">${esc(rangeText)}${item.error ? ` · ${esc(item.error)}` : ""}</div></div><time class="activity-time">${esc(date(item.finished_at || item.started_at))}</time></div>`;
    }).join("") : empty("暂无扫描记录", "运行一次增量扫描后会记录在这里。");
    $("#eventList").innerHTML = state.events.length ? state.events.map(item => `<div class="activity-row"><span class="activity-dot ${item.status === "failed" ? "fail" : item.status === "pending" ? "warn" : ""}"></span><div><div class="activity-title">${esc(item.subject || "邮件事件")} · ${esc(statusName(item.status))}</div><div class="activity-sub">${esc(item.folder)} · 消费尝试 ${esc(item.consumption_count || 0)} 次${item.last_error ? ` · ${esc(item.last_error)}` : ""}</div></div><time class="activity-time">${esc(date(item.created_at))}</time></div>`).join("") : empty("暂无事件", "已扫描的新增内容会形成独立事件。");
  }
  function renderOutbox() {
    const query = state.search.trim().toLowerCase();
    const list = state.outbox.filter(item => !query || [item.subject, item.body, item.status].join(" ").toLowerCase().includes(query));
    $("#outboxList").innerHTML = list.length ? list.map(item => `<article class="outbox-card"><div class="outbox-head"><div><div class="outbox-title">${esc(item.subject)}</div><div class="outbox-meta">${esc(item.notification_type)} · ${esc(date(item.created_at))}</div></div>${badge(item.status)}</div><div class="outbox-body">${esc(item.body)}</div></article>`).join("") : empty(state.outbox.length ? "没有匹配的通知" : "通知队列为空", state.outbox.length ? "调整搜索词。" : "任务 Agent 生成通知预览后会出现在这里；页面不会自动发送。");
  }
  function initViewMotion() {
    if (viewMotion) { viewMotion.revert(); viewMotion = null; }
    if (!window.gsap || !window.ScrollTrigger) return;
    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    viewMotion = gsap.matchMedia();
    viewMotion.add({ motion: "(prefers-reduced-motion: no-preference)", desktop: "(min-width: 960px)" }, ({ conditions }) => {
      if (!conditions.motion) return;
      if (state.view === "overview") {
        gsap.fromTo(".heading-copy > *", { y: 15, autoAlpha: 0 }, {
          y: 0, autoAlpha: 1, duration: 0.65, stagger: 0.07, ease: "power2.out", clearProps: "transform,opacity,visibility"
        });
        gsap.fromTo(".metric-card", { y: 18, autoAlpha: 0 }, {
          y: 0, autoAlpha: 1, duration: 0.55, stagger: 0.08, delay: 0.18,
          ease: "power2.out", clearProps: "transform,opacity,visibility"
        });
        const visual = document.querySelector("#heroVisual");
        if (visual) gsap.fromTo(visual, { scale: 0.82, autoAlpha: 0.2 }, {
          scale: 1, autoAlpha: 1, ease: "none",
          scrollTrigger: { trigger: ".page-heading", start: "top bottom", end: "bottom top", scrub: 0.8 }
        });
        const ticker = document.querySelector("#signalTrack");
        if (ticker) gsap.to(ticker, { xPercent: -50, duration: 28, repeat: -1, ease: "none" });
      }
      if (state.view === "activity" && conditions.desktop) {
        const rail = document.querySelector(".activity-intro");
        const stream = document.querySelector(".activity-stack");
        if (rail && stream) ScrollTrigger.create({
          trigger: ".activity-layout", start: "top top+=112", endTrigger: stream,
          end: "bottom bottom-=96", pin: rail, pinSpacing: true, anticipatePin: 1, invalidateOnRefresh: true
        });
      }
      requestAnimationFrame(() => ScrollTrigger.refresh());
    });
  }
  function openMail(itemId) {
    const item = state.mail.find(candidate => candidate.item_id === itemId);
    if (!item) return;
    const m = item.metadata || {};
    $("#dialogSubject").textContent = m.subject || "(无主题)";
    $("#dialogMeta").innerHTML = `<span>发件人：${esc(m.from || "未知")}</span><span>收件时间：${esc(date(m.date || item.observed_at))}</span><span>收件人：${esc(m.to || "—")}</span>${(m.attachments || []).length ? `<span>附件：${(m.attachments || []).map(a => esc(a.filename || a.content_type)).join("、")}</span>` : ""}`;
    $("#dialogBody").textContent = item.content_text || "此邮件没有可预览的正文。完整 MIME 原文已按内容哈希保存在本地。";
    $("#dialogFolder").textContent = `${item.folder || "邮件"} · UID ${m.uid ?? "—"}`;
    $("#dialogEventStatus").textContent = statusName(item.event_status || "pending");
    $("#mailDialog").showModal();
  }
  async function changeTaskStatus(taskId, status) {
    try { await postJSON(`/api/tasks/${encodeURIComponent(taskId)}/status`, { status }); toast(`任务状态已更新：${statusName(status)}`); await refreshData(true); }
    catch (error) { toast(error.message || "状态更新失败"); await refreshData(true); }
  }
  function openSubscriptionConfig(scopeId) {
    const form = $("#subscriptionForm");
    const item = state.subscriptions.find(candidate => candidate.id === scopeId);
    if (!item) { toast("找不到这条聊天配置"); return; }
    form.reset();
    state.subscriptionEditingId = item.id;
    $("#subscriptionPlatform").value = item.platform;
    $("#subscriptionPlatform").disabled = true;
    $("#subscriptionAccount").value = item.account_key;
    $("#subscriptionAccount").readOnly = true;
    $("#subscriptionConversation").value = item.conversation_key;
    $("#subscriptionConversation").readOnly = true;
    $("#subscriptionName").value = item.display_name;
    $("#subscriptionName").readOnly = true;
    $("#subscriptionEnabled").checked = item.enabled;
    $("#subscriptionFullScan").checked = item.full_scan_enabled;
    subscriptionTagDraft = (item.tags || []).map(t => t.name);
    renderSubscriptionTags();
    $("#subscriptionConfigTitle").textContent = "调整扫描范围";
    $("#subscriptionConfigDescription").textContent = item.display_name + " · " + (item.platform === "wechat" ? "微信" : "QQ") + " · 由扫描器维护会话标识";
    $("#subscriptionConfigDialog").showModal();
  }
  function renderSubscriptionTags() {
    const key = name => name.normalize("NFC").toLowerCase();
    const pool = new Map(state.chatTags.map(tag => [key(tag.name), tag.name]));
    subscriptionTagDraft.forEach(name => pool.set(key(name), name));
    $("#subscriptionTagChoices").innerHTML = pool.size ? Array.from(pool.values()).map(name => {
      const selected = subscriptionTagDraft.some(value => key(value) === key(name));
      return `<button type="button" class="chat-tag-choice ${selected ? "is-selected" : ""}" aria-pressed="${selected}" data-subscription-tag="${esc(name)}">${esc(name)}${selected ? ' ✓' : ''}</button>`;
    }).join("") : '<span class="panel-meta">还没有标签，在下方创建第一个。</span>';
    $("#subscriptionTagFeedback").textContent = subscriptionTagDraft.length ? `已选 ${subscriptionTagDraft.length} 个：${subscriptionTagDraft.join("、")}` : "未选择标签";
  }
  function addSubscriptionTag() {
    const input = $("#subscriptionTagName");
    const name = input.value.normalize("NFC").trim();
    if (!name || Array.from(name).length > 32 || /[\u0000-\u001f\u007f]/.test(name)) { toast("标签须为 1–32 个字符，不能含控制字符"); return; }
    const existing = state.chatTags.find(tag => tag.name.toLowerCase() === name.toLowerCase());
    if (!subscriptionTagDraft.some(value => value.toLowerCase() === name.toLowerCase())) {
      if (subscriptionTagDraft.length >= 20) { toast("每个聊天最多 20 个标签"); return; }
      subscriptionTagDraft.push(existing ? existing.name : name);
    }
    input.value = "";
    renderSubscriptionTags();
  }
  async function saveSubscription(form) {
    if (!state.subscriptionEditingId) { toast("找不到要配置的会话"); return; }
    if ($("#subscriptionTagName").value.trim()) {
      addSubscriptionTag();
      if ($("#subscriptionTagName").value.trim()) return;
    }
    const payload = {
      enabled: $("#subscriptionEnabled").checked,
      full_scan_enabled: $("#subscriptionFullScan").checked,
      tags: [...subscriptionTagDraft]
    };
    try {
      await postJSON("/api/subscriptions/" + encodeURIComponent(state.subscriptionEditingId) + "/settings", payload);
      $("#subscriptionConfigDialog").close();
      form.reset();
      state.subscriptionEditingId = null;
      toast("聊天范围配置已保存");
      await refreshData(true);
    } catch (error) { toast(error.message || "保存配置失败"); }
  }
  async function scanChatCandidates(platform) {
    const button = $$("[data-discover-chat]").find(node => node.dataset.discoverChat === platform);
    const original = button.textContent;
    button.disabled = true;
    button.textContent = platform === "wechat" ? "正在扫描微信…" : "正在扫描 QQ…";
    try {
      const result = await postJSON("/api/chat-discovery", { platform });
      toast((platform === "wechat" ? "微信" : "QQ") + "会话发现完成：" + (result.candidates || []).length + " 个候选会话");
      await refreshData(true);
    } catch (error) {
      toast(error.message || "会话扫描失败");
      await refreshData(true);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }
  async function saveChatSelections() {
    if(state.chatTransferSaving || !(state.chatAdded.size || state.chatRemoved.size))return;
    const payload={candidate_ids:[],enable_ids:[],disable_ids:[],full_scan_enabled:$("#newChatsFullScan").checked};
    for(const key of state.chatAdded) {
      const item=state.chatCatalog.get(key);
      if(item?.scope_id)payload.enable_ids.push(item.scope_id);
      else if(item?.candidate_id)payload.candidate_ids.push(item.candidate_id);
      else{toast('部分发现结果已失效，请重新扫描');return;}
    }
    for(const key of state.chatRemoved)payload.disable_ids.push(state.chatCatalog.get(key).scope_id);
    state.chatTransferSaving=true;renderChatDiscovery();
    $("#saveChatSelections").textContent='正在保存…';
    try {
      const result=await postJSON('/api/subscriptions/transfer',payload);
      state.subscriptions=result.subscriptions;
      state.chatAdded.clear();state.chatRemoved.clear();
      state.chatChecks.available.clear();state.chatChecks.selected.clear();
      toast(`已保存：启用 ${result.added+result.enabled} 个，停用 ${result.disabled} 个；未导入正文`);
      document.dispatchEvent(new CustomEvent('chat:subscriptions-changed'));
      await refreshData(true);
    }catch(error){toast(error.message || '保存失败，变更尚未生效');}
    finally{state.chatTransferSaving=false;$("#saveChatSelections").textContent='保存订阅变更';renderChatDiscovery();}
  }
  async function scanMail() {
    if (!window.confirm("现在对已保存配置的 QQ 邮箱执行只读增量扫描？不会发送邮件。")) return;
    const button = $("#scanButton"), label = button.innerHTML;
    button.disabled = true; button.innerHTML = '<span class="scan-glyph">↻</span><span>正在扫描…</span>';
    try {
      const result = await postJSON("/api/scan", {});
      const added = (result.folders || []).reduce((sum, folder) => sum + Number(folder.new_count || 0), 0);
      toast(result.ok ? `扫描完成，新增 ${added} 封邮件` : "扫描完成，但有文件夹失败");
      await refreshData(true);
    } catch (error) { toast(error.message || "邮箱扫描失败"); }
    finally { button.disabled = false; button.innerHTML = label; }
  }
  async function scanMailRange() {
    const start = $("#rangeStart").value, end = $("#rangeEnd").value;
    if (!start || !end) { toast("请先选择开始和结束日期"); return; }
    if (end < start) { toast("结束日期不能早于开始日期"); return; }
    if (!window.confirm(`对所有邮箱文件夹全量扫描 ${start} 至 ${end}（北京时间，含结束日期）？已存在邮件会自动去重，增量游标不变。`)) return;
    const button = $("#rangeScanButton"), label = button.textContent;
    button.disabled = true; button.textContent = "正在全量扫描…";
    try {
      const result = await postJSON("/api/scan-range", { start, end });
      const matched = (result.folders || []).reduce((sum, folder) => sum + Number(folder.matched_count || 0), 0);
      const inserted = (result.folders || []).reduce((sum, folder) => sum + Number(folder.new_count || 0), 0);
      toast(result.ok ? `范围扫描完成：命中 ${matched} 封，新增 ${inserted} 封` : "范围扫描完成，但有文件夹失败");
      await refreshData(true);
      if (!result.ok) setView("activity");
    } catch (error) { toast(error.message || "范围扫描失败"); }
    finally { button.disabled = false; button.textContent = label; }
  }
  $$(".nav-item").forEach(node => node.addEventListener("click", () => setView(node.dataset.view)));
  $$('[data-jump]').forEach(node => node.addEventListener("click", () => setView(node.dataset.jump)));
  $("#refreshButton").addEventListener("click", () => refreshData(false));
  $("#scanButton").addEventListener("click", scanMail);
  $("#rangeScanButton").addEventListener("click", scanMailRange);
  $$('[data-mail-carousel]').forEach(button => button.addEventListener("click", () => {
    state.mailCarouselOffset = Math.max(0, state.mailCarouselOffset + (button.dataset.mailCarousel === "next" ? 2 : -2));
    renderOverview();
  }));
  $("#subscriptionForm").addEventListener("submit", event => { event.preventDefault(); saveSubscription(event.currentTarget); });
  document.addEventListener("chat:imported", () => refreshData(true));
  $("#addSubscriptionTag").addEventListener("click", addSubscriptionTag);
  $("#subscriptionTagName").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); addSubscriptionTag(); } });
  $("#subscriptionTagChoices").addEventListener("click", event => {
    const button = event.target.closest("[data-subscription-tag]");
    if (!button) return;
    const name = button.dataset.subscriptionTag;
    const index = subscriptionTagDraft.findIndex(value => value.toLowerCase() === name.toLowerCase());
    if (index >= 0) subscriptionTagDraft.splice(index, 1);
    else if (subscriptionTagDraft.length >= 20) { toast("每个聊天最多 20 个标签"); return; }
    else subscriptionTagDraft.push(name);
    renderSubscriptionTags();
  });
  $("#subscriptionTagFilter").addEventListener("change", event => { state.subscriptionTagFilter = event.target.value; renderSubscriptions(); });
  $$('[data-discover-chat]').forEach(button => button.addEventListener("click", () => scanChatCandidates(button.dataset.discoverChat)));
  $("#saveChatSelections").addEventListener("click", saveChatSelections);
  $("#chatDiscoverySearch").addEventListener("input",renderChatDiscovery);
  $("#chatDiscoveryPlatform").addEventListener("change",renderChatDiscovery);
  $("#chatDiscoveryType").addEventListener("change",renderChatDiscovery);
  $("#chatSelectedSearch").addEventListener('input',renderChatDiscovery);
  $("#transferAdd").addEventListener('click',()=>moveChatSelections(true));
  $("#transferRemove").addEventListener('click',()=>moveChatSelections(false));
  $("#resetChatSelections").addEventListener('click',()=>{state.chatAdded.clear();state.chatRemoved.clear();state.chatChecks.available.clear();state.chatChecks.selected.clear();renderChatDiscovery();});
  document.addEventListener('change',event=>{
    if(state.chatTransferSaving)return;
    if(event.target.matches('[data-transfer-side]')) {
      const checks=state.chatChecks[event.target.dataset.transferSide],key=event.target.dataset.transferKey;
      if(event.target.checked)checks.add(key);else checks.delete(key);
      renderChatDiscovery();
    }
    if(['transferAllLeft','transferAllRight'].includes(event.target.id)) {
      const checks=state.chatChecks[event.target.id==='transferAllLeft'?'available':'selected'];
      for(const key of JSON.parse(event.target.dataset.keys || '[]')) {
        if(event.target.checked)checks.add(key);else checks.delete(key);
      }
      renderChatDiscovery();
    }
  });
  $("#closeSubscriptionConfig").addEventListener("click", () => $("#subscriptionConfigDialog").close());
  $("#cancelSubscriptionConfig").addEventListener("click", () => $("#subscriptionConfigDialog").close());
  $("#subscriptionConfigDialog").addEventListener("click", event => { if (event.target === event.currentTarget) event.currentTarget.close(); });
  $("#closeDialog").addEventListener("click", () => $("#mailDialog").close());
  $("#searchInput").addEventListener("input", event => { state.search = event.target.value; renderCurrent(); });
  document.addEventListener("click", event => {
    const mail = event.target.closest("[data-mail-id]");
    if (mail) openMail(mail.dataset.mailId);
    const complete = event.target.closest("[data-complete]");
    if (complete) changeTaskStatus(complete.dataset.complete, "completed");
    const configure = event.target.closest("[data-config-subscription]");
    if (configure) openSubscriptionConfig(configure.dataset.configSubscription);
  });
  document.addEventListener("change", event => {
    if (event.target.matches("[data-task-status]")) changeTaskStatus(event.target.dataset.taskStatus, event.target.value);
  });
  $$("[data-task-filter]").forEach(button => button.addEventListener("click", () => {
    state.taskFilter = button.dataset.taskFilter;
    $$("[data-task-filter]").forEach(item => item.classList.toggle("selected", item === button));
    renderTasks();
  }));
  $("#searchInput").addEventListener("keydown", event => { if (event.key === "Escape") { event.target.value = ""; state.search = ""; renderCurrent(); } });
  document.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#searchInput").focus(); } });
  setView("overview");
  refreshData(true);
})();
