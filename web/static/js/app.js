const $ = id => document.getElementById(id);
const TOKEN = document.querySelector('meta[name="web-token"]')?.content?.trim() || '';

/* ── REV 1.3.16: attribute-safe HTML escape ── */
const esc = s => String(s ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

const nowTime = () => new Date().toLocaleTimeString();

/* pixel-perfect money formatting */
const fmtMoney = (v, signed=false) => {
  const n = Number(v) || 0;
  const abs = Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
  if(signed) return (n < 0 ? '-' : '+') + '$' + abs;
  return (n < 0 ? '-' : '') + '$' + abs;
};

/* FIX #2: central rich empty-state helper — consistent across all tabs */
function emptyRow(colspan, title, sub){
  return `<tr class="empty-row"><td colspan="${colspan}">
    <div class="empty-state">
      <div class="empty-state-title">${esc(title)}</div>
      ${sub ? `<div class="empty-state-sub">${esc(sub)}</div>` : ''}
    </div>
  </td></tr>`;
}

/* ══════════════════════════════════════════════════════════════
   RUNTIME CONFIG
   ══════════════════════════════════════════════════════════════ */
let CFG = null;

function paintModeOffline(reason){
  const mc = $('mode-chip');
  if(!mc || CFG) return;
  mc.textContent = 'OFFLINE';
  mc.classList.remove('mode-demo','mode-testnet','mode-live','mode-dry');
  mc.classList.add('mode-offline');
  mc.title = reason || 'Config unavailable';
}

async function fetchConfig(){
  try{
    const r = await fetch('/api/config', {
      headers: TOKEN ? { 'Authorization': 'Bearer ' + TOKEN } : {}
    });
    if(!r.ok){
      console.warn('[config] HTTP', r.status);
      paintModeOffline(`Config unavailable (HTTP ${r.status})`);
      return null;
    }
    CFG = await r.json();
    applyConfigToUI();
    return CFG;
  }catch(e){
    console.warn('[config] fetch failed', e);
    paintModeOffline('Config fetch failed — see console');
    return null;
  }
}

function applyConfigToUI(){
  if(!CFG) return;

  const mc = $('mode-chip');
  if(mc){
    mc.classList.remove('mode-demo','mode-testnet','mode-live','mode-dry','mode-offline');
    let label, cls, title;
    if(CFG.demo_mode){
      label = 'DEMO'; cls = 'mode-demo'; title = 'Binance demo environment (fake funds)';
    } else if(CFG.testnet){
      label = 'TESTNET'; cls = 'mode-testnet'; title = 'Binance testnet (fake funds)';
    } else {
      label = 'LIVE'; cls = 'mode-live'; title = '⚠ MAINNET — REAL MONEY';
    }
    if(CFG.dry_run){
      label += ' · DRY';
      cls = 'mode-dry';
      title += ' · DRY_RUN (no orders sent)';
    }
    mc.textContent = label;
    mc.classList.add(cls);
    mc.title = title;
  }

  const dl = Number($('daily-loss')?.dataset?.dl || 0);
  const cap = Number(CFG.max_daily_loss_trades) || 3;
  if($('daily-loss')) $('daily-loss').innerText = dl + ' / ' + cap;
  if($('daily-loss-card')) $('daily-loss-card').classList.toggle('stat-danger', dl >= cap);
  const fill = $('risk-bar-fill');
  if(fill){
    fill.style.width = Math.min(100, (dl / cap) * 100) + '%';
    fill.style.background = dl >= cap ? 'var(--red)' : 'var(--gold)';
  }

  if($('positions') && CFG.max_open_positions){
    const card = $('positions').closest('.stat');
    if(card) card.title = `Max ${CFG.max_open_positions} concurrent positions`;
  }

  if(CFG.leverage && CFG.risk_percent){
    const chip = document.querySelector('.stat-accent .chip-mini');
    if(chip) chip.title = `Lev ${CFG.leverage}× · Risk ${CFG.risk_percent}% / trade`;
  }

  if($('start-btn') && CFG.trading_mode){
    $('start-btn').title = `Start on ${CFG.trading_mode} · Lev ${CFG.leverage}× · Risk ${CFG.risk_percent}%`;
  }

  const pc = $('partial-chip');
  if(pc){
    const val = Number(CFG.partial_close_usdt || 0);
    if(val > 0){
      pc.textContent = `PARTIAL $${val.toFixed(0)}`;
      pc.title = `Auto partial-close at $${val.toFixed(2)} unrealized profit (70% locked)`;
      pc.classList.remove('hidden');
    } else {
      pc.classList.add('hidden');
      pc.title = 'Partial close disabled (PARTIAL_CLOSE_USDT=0)';
    }
  }

  const lb = $('history-lookback-chip');
  if(lb && CFG.history_lookback_days){
    lb.textContent = `${CFG.history_lookback_days}d window`;
    lb.title = `History window: ${CFG.history_lookback_days} days (HISTORY_LOOKBACK_DAYS)`;
  }
}

/* ══════════════════════════════════════════════════════════════
   TOAST / SKELETON
   ══════════════════════════════════════════════════════════════ */
function toast(msg, type='info'){
  const c = $('toast-container'); if(!c) return;
  const ic = type==='success' ? '✓' : type==='error' ? '✕' : 'ℹ';
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<span class="toast-ic">${ic}</span><span>${esc(msg)}</span>`;
  c.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{ el.classList.remove('show'); setTimeout(()=>el.remove(),250); }, 4200);
}

function skeletonRows(cols, n=4){
  let rows='';
  for(let i=0;i<n;i++){
    rows += `<tr class="sk-row">` + Array.from({length:cols}).map(()=>`<td><div class="sk-bar" style="width:${40+Math.random()*40}%"></div></td>`).join('') + `</tr>`;
  }
  return rows;
}

/* ══════════════════════════════════════════════════════════════
   TABS
   ══════════════════════════════════════════════════════════════ */
function setTab(name){
  document.querySelectorAll('.tab-panel').forEach(el=>el.classList.add('hidden'));
  document.querySelectorAll('.tab-btn').forEach(b=>{
    b.classList.remove('active');
    b.setAttribute('aria-selected','false');
    b.tabIndex = -1;
  });
  $(`tab-${name}`)?.classList.remove('hidden');
  const activeBtn = document.querySelector(`[data-tab="${name}"]`);
  activeBtn?.classList.add('active');
  activeBtn?.setAttribute('aria-selected','true');
  if(activeBtn) activeBtn.tabIndex = 0;
  if(name==='history') fetchHistory();
  if(name==='logs') fetchLogs();
  if(name==='scanner') fetchScan();
  if(name==='fills') fetchFills();
}
const tabBtns = Array.from(document.querySelectorAll('.tab-btn'));
tabBtns.forEach((btn,i)=>{
  btn.addEventListener('click', ()=> setTab(btn.dataset.tab));
  btn.addEventListener('keydown', e=>{
    let target = null;
    if(e.key==='ArrowRight') target = tabBtns[(i+1) % tabBtns.length];
    else if(e.key==='ArrowLeft') target = tabBtns[(i-1+tabBtns.length) % tabBtns.length];
    else if(e.key==='Home') target = tabBtns[0];
    else if(e.key==='End') target = tabBtns[tabBtns.length-1];
    if(target){ e.preventDefault(); target.focus(); setTab(target.dataset.tab); }
  });
});
$('clear-logs')?.addEventListener('click', ()=> { lastLogs=[]; if($('log-container')) $('log-container').innerHTML=''; });

/* ══════════════════════════════════════════════════════════════
   FILTERS
   ══════════════════════════════════════════════════════════════ */
function applyFilter(tbodyId, inputId){
  const inp = $(inputId); if(!inp) return;
  const val = inp.value.trim().toUpperCase();
  const rows = $(tbodyId)?.querySelectorAll('tr[data-sym]') || [];
  rows.forEach(r=>{
    const sym = String(r.dataset.sym || '').toUpperCase();
    r.style.display = (!val || sym.includes(val)) ? '' : 'none';
  });
}
$('live-filter')?.addEventListener('input', ()=>applyFilter('trades-body','live-filter'));
$('scan-filter')?.addEventListener('input', ()=>applyFilter('scan-body','scan-filter'));
$('history-filter')?.addEventListener('input', ()=>applyFilter('pnl-body','history-filter'));
$('fills-filter')?.addEventListener('input', ()=>applyFilter('fills-body','fills-filter'));

/* ══════════════════════════════════════════════════════════════
   REFRESH BUTTONS
   ══════════════════════════════════════════════════════════════ */
$('live-refresh')?.addEventListener('click', e=>{ e.currentTarget.classList.add('spinning'); fetchStatus().finally(()=>e.currentTarget.classList.remove('spinning')); });
$('scan-refresh')?.addEventListener('click', e=>{ e.currentTarget.classList.add('spinning'); fetchScan().finally(()=>e.currentTarget.classList.remove('spinning')); });
$('history-refresh')?.addEventListener('click', e=>{ e.currentTarget.classList.add('spinning'); fetchHistory().finally(()=>e.currentTarget.classList.remove('spinning')); });
$('fills-refresh')?.addEventListener('click', e=>{ e.currentTarget.classList.add('spinning'); fetchFills().finally(()=>e.currentTarget.classList.remove('spinning')); });

/* ══════════════════════════════════════════════════════════════
   API + ENGINE CONTROL
   ══════════════════════════════════════════════════════════════ */
async function apiFetch(url, opts={}){
  const headers = opts.headers || {};
  if(TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  return fetch(url, {...opts, headers});
}
async function startBot(){
  const btn=$('start-btn'), label=$('start-btn-label'), sp=$('start-spinner');
  if(!TOKEN){ toast('WEB_TOKEN not set in .env — unauthorized','error'); return; }
  btn.disabled=true; label.innerText='Starting...'; sp.classList.remove('hidden');
  try{
    const r=await apiFetch('/api/start',{method:'POST'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'Start failed');
    toast('Bot engine started','success');
  }catch(e){ toast(e.message,'error'); }
  finally{ btn.disabled=false; label.innerText='Start Engine'; sp.classList.add('hidden'); fetchStatus(); }
}
async function stopBot(){
  const btn=$('stop-btn'), label=$('stop-btn-label'), sp=$('stop-spinner');
  if(!TOKEN){ toast('WEB_TOKEN missing','error'); return; }
  btn.disabled=true; label.innerText='Stopping...'; sp.classList.remove('hidden');
  try{
    const r=await apiFetch('/api/stop',{method:'POST'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'Stop failed');
    toast('Bot stopping...','info');
  }catch(e){ toast(e.message,'error'); }
  finally{ btn.disabled=false; label.innerText='Stop'; sp.classList.add('hidden'); fetchStatus(); }
}

let pendingAction = null;
let modalOpenerEl = null;

function openConfirm(action){
  pendingAction = action;
  $('confirm-title').innerText = action==='start' ? 'Start Trading Engine?' : 'Stop Trading Engine?';
  const isLive = !CFG || (!CFG.demo_mode && !CFG.testnet && !CFG.dry_run);
  if(action==='start'){
    $('confirm-message').innerText = isLive
      ? '⚠ MAINNET — REAL MONEY. This will arm the strategy engine and begin live order execution. Confirm you want to proceed.'
      : 'This will arm the strategy engine and begin order execution on the configured account. Confirm you want to proceed.';
  } else {
    $('confirm-message').innerText = 'This halts scanning and new order execution. Any open positions will remain live on the exchange until closed manually.';
  }
  $('confirm-ok').className = action==='start' ? 'btn btn-start' : 'btn btn-danger';
  $('confirm-ok').innerText = action==='start' ? 'Start Engine' : 'Stop Engine';

  modalOpenerEl = document.activeElement;

  const m = $('confirm-modal');
  m.classList.add('show');
  m.setAttribute('aria-hidden', 'false');

  $('confirm-cancel').focus();
}

function closeConfirm(){
  const m = $('confirm-modal');
  m.classList.remove('show');
  m.setAttribute('aria-hidden', 'true');

  pendingAction = null;
  modalOpenerEl?.focus();
}

$('start-btn').addEventListener('click', ()=>openConfirm('start'));
$('stop-btn').addEventListener('click', ()=>openConfirm('stop'));
$('confirm-cancel').addEventListener('click', closeConfirm);
$('confirm-modal').addEventListener('click', e=>{ if(e.target.id==='confirm-modal') closeConfirm(); });
$('confirm-ok').addEventListener('click', ()=>{
  const action = pendingAction; closeConfirm();
  if(action==='start') startBot();
  else if(action==='stop') stopBot();
});
document.addEventListener('keydown', e=>{
  if(e.key==='Escape' && $('confirm-modal').classList.contains('show')) closeConfirm();
});
$('confirm-modal').addEventListener('keydown', e=>{
  if(e.key!=='Tab') return;
  const focusables = $('confirm-modal').querySelectorAll('button');
  const first = focusables[0], last = focusables[focusables.length-1];
  if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
});

/* ══════════════════════════════════════════════════════════════
   CONNECTION HEALTH
   ══════════════════════════════════════════════════════════════ */
function setConnHealth(ok){
  const d=$('conn-dot'), t=$('conn-text');
  if(!d||!t) return;
  if(ok){ d.classList.remove('offline'); t.innerText='LIVE'; t.style.color='var(--ink-dim)'; }
  else { d.classList.add('offline'); t.innerText='OFFLINE'; t.style.color='var(--red)'; }
}

/* ══════════════════════════════════════════════════════════════
   STATUS
   ══════════════════════════════════════════════════════════════ */
async function fetchStatus(){
 try{
  const r=await fetch('/api/status'); const d=await r.json();
  setConnHealth(true);
  $('balance').innerText=fmtMoney(d.balance||0);

  const dl = Number(d.daily_loss||0);
  const cap = (CFG && Number(CFG.max_daily_loss_trades)) || 3;
  $('daily-loss').innerText = dl + ' / ' + cap;
  $('daily-loss').dataset.dl = dl;
  $('daily-loss-card').classList.toggle('stat-danger', dl >= cap);
  const riskFill = $('risk-bar-fill');
  if(riskFill){
    riskFill.style.width = Math.min(100, (dl/cap)*100) + '%';
    riskFill.style.background = dl >= cap ? 'var(--red)' : 'var(--gold)';
  }

  $('positions').innerText=d.position_count||0;
  $('live-count-tab').innerText=d.position_count||0;
  $('last-update').innerText=d.last_update||'--';
  $('uptime').innerText=d.uptime||'00:00:00';

  const kb=$('keys-badge'); const kbt=$('keys-badge-text');
  if(kb && kbt){
    if(d.keys_set){ kbt.innerText='KEYS LINKED'; kb.style.color='var(--green)'; kb.style.borderColor='var(--green-line)'; kb.style.background='var(--green-soft)'; }
    else { kbt.innerText='KEYS NOT SET'; kb.style.color='var(--ink-faint)'; kb.style.borderColor='var(--line)'; kb.style.background='rgba(255,255,255,0.03)'; }
  }

  const tb=$('trades-body');
  if(d.active_trades?.length){
   tb.innerHTML = d.active_trades.map(t => {
       let slDisplay = t.sl_level ?? t.sl ?? 'INITIAL';
       if (typeof slDisplay !== 'string') slDisplay = String(slDisplay);
       const side = (t.side||'').toUpperCase();
       const entry = Number(t.entry||0).toFixed(4);
       const mark = Number(t.mark||0).toFixed(4);
       const qty = Number(t.qty||0);
       const pnl = Number(t.pnl||0);
       const pnl_pct = Number(t.pnl_pct||0);
       const isManual = !!t.is_manual;
       return `<tr data-sym="${esc((t.symbol||'').toUpperCase())}">
  <td><div class="cell-inner left"><span class="val sym-val">${esc(t.symbol)}</span></div></td>
  <td><div class="cell-inner center"><span class="badge ${side==='LONG'?'badge-buy':'badge-sell'}">${esc(side)}</span></div></td>
  <td><div class="cell-inner right"><span class="val">$${entry}</span></div></td>
  <td><div class="cell-inner right"><span class="val">$${mark}</span></div></td>
  <td><div class="cell-inner right"><span class="val">${qty}</span></div></td>
  <td><div class="cell-inner right"><span class="pnl-chip ${pnl>=0?'pnl-chip-pos':'pnl-chip-neg'}">${pnl>=0?'▲':'▼'} ${pnl>=0?'+':''}$${pnl.toFixed(2)} <span style="opacity:.7;font-size:10px">(${pnl>=0?'+':''}${pnl_pct.toFixed(2)}%)</span></span></div></td>
  <td><div class="cell-inner center"><span class="sl-badge">${esc(slDisplay)}</span></div></td>
  <td><div class="cell-inner center"><span class="badge ${isManual?'badge-manual':'badge-bot'}">${isManual?'MANUAL':'BOT'}</span></div></td>
</tr>`;
   }).join('');
   applyFilter('trades-body','live-filter');
  } else {
    /* FIX #2: rich empty state instead of plain dash */
    tb.innerHTML = emptyRow(
      9,
      'No positions open',
      'Market scanner is checking for setups. Best opportunities come during London (12:00 PKT) and NY (17:00 PKT) sessions.'
    );
  }

  const b = $('status-badge');
  const bt = $('status-badge-text');
  const dot = b?.querySelector('.dot');
  if(d.running){
    b?.classList.remove('stopped');
    if(bt) bt.innerText = 'RUNNING';
    if(dot){ dot.style.background = 'var(--green)'; dot.classList.add('blink'); }
    $('start-btn').disabled = true;
    $('stop-btn').disabled = false;
  } else {
    b?.classList.add('stopped');
    if(bt) bt.innerText = 'STOPPED';
    if(dot){ dot.style.background = '#6b7280'; dot.classList.remove('blink'); }
    $('start-btn').disabled = false;
    $('stop-btn').disabled = true;
  }
  if($('live-sync')) $('live-sync').innerText='updated '+nowTime();
  return d;
 }catch(e){ console.warn('status',e); setConnHealth(false); return null; }
}

/* ══════════════════════════════════════════════════════════════
   SCANNER
   ══════════════════════════════════════════════════════════════ */
function _regimeChipClass(regime){
  const r = String(regime||'').toUpperCase();
  if(r === 'CHOP') return 'regime-chop';
  if(r === 'VOLATILE') return 'regime-volatile';
  if(r === 'QUIET') return 'regime-quiet';
  if(r.startsWith('TREND')) return 'regime-trend';
  return 'regime-unknown';
}

function _familyChipClass(family){
  const f = String(family||'').toLowerCase();
  if(f.includes('trend'))      return 'family-trend';
  if(f.includes('momentum'))   return 'family-momentum';
  if(f.includes('range'))      return 'family-range';
  if(f.includes('volatility') || f.includes('volatile')) return 'family-volatile';
  return 'family-unknown';
}
function _familyLabel(family){
  const f = String(family||'').replace(/_coins$/i, '');
  return f ? f.charAt(0).toUpperCase() + f.slice(1) : '—';
}

function _updateHeaderChips(scanResults){
  const rEl = $('regime-chip');
  if(rEl){
    const clsAll = ['regime-trend','regime-chop','regime-volatile','regime-quiet','regime-unknown'];
    rEl.classList.remove(...clsAll);
    if(Array.isArray(scanResults) && scanResults.length){
      const counts = {};
      scanResults.forEach(r => {
        const k = String(r.regime || 'UNKNOWN').toUpperCase();
        counts[k] = (counts[k]||0) + 1;
      });
      const dominant = Object.keys(counts).sort((a,b)=>counts[b]-counts[a])[0] || '--';
      rEl.textContent = 'REGIME ' + dominant;
      rEl.classList.add(_regimeChipClass(dominant));
      rEl.title = 'Dominant regime across ' + scanResults.length + ' coins';
    } else {
      rEl.textContent = 'REGIME --';
      rEl.classList.add('regime-unknown');
      rEl.title = 'No scan results yet';
    }
  }

  const kEl = $('killzone-chip');
  if(kEl){
    if(Array.isArray(scanResults) && scanResults.length){
      const kz = String(scanResults[0].killzone || 'NONE').toUpperCase();
      kEl.textContent = 'KZ ' + kz;
      kEl.classList.toggle('active', kz !== 'NONE');
      kEl.title = kz === 'NONE'
        ? 'Outside major session (off-hours)'
        : 'Active ' + kz + ' session';
    } else {
      kEl.textContent = 'KZ --';
      kEl.classList.remove('active');
      kEl.title = 'No scan results yet';
    }
  }
}

async function fetchScan(){
 try{
  const tb=$('scan-body');
  if(!tb.dataset.loaded) tb.innerHTML = skeletonRows(13,5);
  const r=await fetch('/api/scan'); const data=await r.json();
  const ts = data.timestamp || '--';
  $('scan-time').innerText=ts!=='Bot Stopped'? '⏱ '+ts : ts;
  if($('scan-time-full')) $('scan-time-full').innerText=ts;

  if(data.results?.length){
   tb.dataset.loaded='1';
   tb.innerHTML=data.results.map(rw=>{
    const getBadge = s=>{
      if(!s) return 'badge-neutral';
      s=String(s).toUpperCase();
      if(s==='BUY') return 'badge-buy';
      if(s==='WEAK_BUY' || s==='WEAK BUY') return 'badge-weakbuy';
      if(s==='SELL') return 'badge-sell';
      if(s==='WEAK_SELL' || s==='WEAK SELL') return 'badge-weaksell';
      if(s.includes('BUY')) return 'badge-weakbuy';
      if(s.includes('SELL')) return 'badge-weaksell';
      return 'badge-neutral';
    };
    const fmtSig = s=> esc((s||'-').replaceAll('_',' '));
    const confNum = Math.max(0, Number(rw.conf)||0);
    const confVal = confNum.toFixed(1);
    let confCls='low', confColor='#3a414e';
    if(confNum>=60){ confCls='high'; confColor='#00d07a'; }
    else if(confNum>=40){ confCls='mid'; confColor='#d4a853'; }
    else if(confNum>=25){ confCls='low'; confColor='#8a94a6'; }
    const adxVal = Number(rw.adx)||0;
    const priceNum = Number(rw.price||0);
    const priceStr = priceNum < 1 ? priceNum.toFixed(2) : priceNum.toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
    const isV2 = String(rw.action||'').includes('V2-');

    const regimeRaw = String(rw.regime || '--').toUpperCase();
    const regimeCls = _regimeChipClass(regimeRaw);
    const kzRaw = String(rw.killzone || 'NONE').toUpperCase();
    const kzActive = kzRaw !== 'NONE';

    const familyRaw = rw.family || 'unknown';
    const familyCls = _familyChipClass(familyRaw);
    const familyLabel = _familyLabel(familyRaw);

    return `<tr data-sym="${esc((rw.symbol||'').toUpperCase())}">
      <td><div class="cell-inner left"><span class="asset-sym">${esc(rw.symbol)}</span></div></td>
      <td><div class="cell-inner center"><span class="badge ${getBadge(rw.sig_1h)}">${fmtSig(rw.sig_1h)}</span></div></td>
      <td><div class="cell-inner center"><span class="badge ${getBadge(rw.sig_4h)}">${fmtSig(rw.sig_4h)}</span></div></td>
      <td><div class="cell-inner center"><span class="badge ${getBadge(rw.sig_1d)}">${fmtSig(rw.sig_1d)}</span></div></td>
      <td><div class="cell-inner right"><div class="conf-wrap"><span class="conf-val ${confCls}">${confVal}%</span><div class="conf-track"><div class="conf-fill" style="width:${Math.min(100,confNum)}%;background:${confColor}"></div></div></div></div></td>
      <td><div class="cell-inner right"><span class="val price-val">$${priceStr}</span></div></td>
      <td><div class="cell-inner right"><span class="val rr-val">${Number(rw.rr||0).toFixed(2)}</span></div></td>
      <td><div class="cell-inner right"><span class="adx-pill">${adxVal.toFixed(1)}</span></div></td>
      <td><div class="cell-inner left"><span class="pattern-val">${esc(rw.pattern||'None')}</span></div></td>
      <td><div class="cell-inner center"><span class="chip chip-mini ${familyCls}" title="${esc(familyRaw)}">${esc(familyLabel)}</span></div></td>
      <td><div class="cell-inner center"><span class="chip chip-mini ${regimeCls}">${esc(regimeRaw)}</span></div></td>
      <td><div class="cell-inner center"><span class="chip chip-mini killzone-chip ${kzActive?'active':''}">${esc(kzRaw)}</span></div></td>
      <td><div class="cell-inner left"><span class="action-val ${isV2?'v2':''}" title="${esc(rw.action||'')}">${esc(rw.action||'-')}</span></div></td>
    </tr>`;
   }).join('');
   applyFilter('scan-body','scan-filter');
  } else {
    tb.dataset.loaded='1';
    /* FIX #2: rich empty state */
    const isStopped = ts === 'Bot Stopped';
    tb.innerHTML = emptyRow(
      13,
      isStopped ? 'Bot stopped' : 'Scanning markets...',
      isStopped
        ? 'Start the engine to begin scanning for setups.'
        : 'Bot scan cycle runs every ~20 seconds.'
    );
  }

  _updateHeaderChips(data.results || []);

  if($('scan-sync')) $('scan-sync').innerText='updated '+nowTime();
 }catch(e){ console.warn('scan',e) }
}

/* ══════════════════════════════════════════════════════════════
   LOGS
   ══════════════════════════════════════════════════════════════ */
let lastLogs = [];
let logFilter = 'ALL';
let autoScroll = true;

function renderLogLine(l){
  let text = esc(l);
  text = text.replaceAll(/(\[ERROR\])/gi, '<span class="lvl lvl-error">$1</span>')
             .replaceAll(/(\[WARN\])/gi, '<span class="lvl lvl-warn">$1</span>')
             .replaceAll(/(\[INFO\])/gi, '<span class="lvl lvl-info">$1</span>');
  return `<div class="log-line">${text}</div>`;
}

function renderLogs(){
  const c=$('log-container'); if(!c) return;
  const bottom = c.scrollTop + c.clientHeight >= c.scrollHeight - 20;
  const filtered = logFilter==='ALL' ? lastLogs : lastLogs.filter(l=> String(l).toUpperCase().includes('['+logFilter+']'));
  c.innerHTML = filtered.map(renderLogLine).join('');
  if(autoScroll || bottom) c.scrollTop = c.scrollHeight;
}

(function(){
  const card = $('net-pnl-card');
  if(!card) return;
  const setOpen = open=>{
    card.classList.toggle('popover-open', open);
    card.setAttribute('aria-expanded', String(open));
  };
  card.addEventListener('click', ()=> setOpen(!card.classList.contains('popover-open')));
  card.addEventListener('keydown', e=>{
    if(e.key==='Enter' || e.key===' '){ e.preventDefault(); setOpen(!card.classList.contains('popover-open')); }
    if(e.key==='Escape') setOpen(false);
  });
  document.addEventListener('click', e=>{ if(!card.contains(e.target)) setOpen(false); });
  card.addEventListener('focusout', e=>{ if(!card.contains(e.relatedTarget)) setOpen(false); });
})();

document.querySelectorAll('.log-filter-btn').forEach(b=>{
  b.addEventListener('click', ()=>{
    logFilter = b.dataset.level;
    document.querySelectorAll('.log-filter-btn').forEach(x=>{
      x.classList.remove('active');
      x.setAttribute('aria-pressed','false');
    });
    b.classList.add('active');
    b.setAttribute('aria-pressed','true');
    renderLogs();
  });
});
$('autoscroll-toggle')?.addEventListener('change', e=>{ autoScroll = e.target.checked; });

async function fetchLogs(){
  try{
    const r=await fetch('/api/logs'); const d=await r.json();
    const c=$('log-container'); if(!c) return;
    /* FIX #3: handle empty logs array too (was: only updated when non-empty) */
    if(Array.isArray(d.logs)){
      lastLogs = d.logs;
      renderLogs();
    }
  }catch{}
}

/* ══════════════════════════════════════════════════════════════
   HISTORY — analytics-ready gate
   ══════════════════════════════════════════════════════════════ */
let _analyticsReady = false;

function _analyticsIsReady(d){
  const sum = d && d.analytics_summary;
  if(!sum || typeof sum !== 'object') return false;
  if(Object.keys(sum).length === 0) return false;
  return (
    sum.fills_count !== undefined ||
    sum.orders_count !== undefined ||
    sum.coverage_start !== undefined ||
    sum.trades_total !== undefined
  );
}

function _paintHistoryWarming(){
  const body = $('pnl-body');
  if(body) body.innerHTML = skeletonRows(8, 5);
  if($('history-count')) $('history-count').innerText = '…';
  if($('history-sync')) $('history-sync').innerText = 'analytics warming up…';
  if($('winrate')) $('winrate').innerText = '—';
  if($('net-pnl')){ $('net-pnl').innerText = '—'; $('net-pnl').style.color = 'var(--ink-dim)'; }
  if($('wins')) $('wins').innerText = '—';
  if($('losses')) $('losses').innerText = '—';
  if($('total-pnl')) $('total-pnl').innerText = '—';
  if($('history-total-pnl-chip')){
    $('history-total-pnl-chip').innerText = '—';
    $('history-total-pnl-chip').style.color = 'var(--ink-faint)';
    $('history-total-pnl-chip').style.background = 'rgba(255,255,255,.03)';
    $('history-total-pnl-chip').style.borderColor = 'var(--line)';
  }
  /* FIX #1: unhide verify-badge when writing content */
  if($('verify-badge')){
    $('verify-badge').classList.remove('hidden');
    $('verify-badge').innerHTML = '<span class="verify-warn">⏳</span>';
  }
  if($('history-verify-text')) $('history-verify-text').innerHTML =
    '<span style="color:var(--ink-mute);font-size:11px">first refresh in progress (~30s)</span>';
  if($('ratio-bar-fill')) $('ratio-bar-fill').style.width = '0%';
  if($('pnl-foot')) $('pnl-foot').classList.add('hidden');
}

/* FIX #5: cache history data so the sparkline wrapper doesn't refetch */
let _lastHistoryData = null;

async function fetchHistory(){
 try{
  const body=$('pnl-body');
  const r=await fetch('/api/history'); const d=await r.json();
  _lastHistoryData = d;

  if(!_analyticsIsReady(d)){
    _analyticsReady = false;
    _paintHistoryWarming();
    return;
  }
  _analyticsReady = true;

  const history = Array.isArray(d.income_history) ? d.income_history : [];
  const analyticsSum = d.analytics_summary || {};
  const fromTrades   = analyticsSum.from_trades || {};
  const fromIncome   = analyticsSum.from_income || {};

  const backendTotal   = Number(fromTrades.gross_pnl  ?? d.total_pnl        ?? 0);
  const backendComm    = Number(fromTrades.commission ?? d.total_commission ?? 0);
  const totalFunding   = Number(fromTrades.funding    ?? d.total_funding    ?? 0);
  const backendNet     = Number(fromTrades.net_pnl    ?? d.net_pnl          ?? 0);

  const backendWins    = Number(analyticsSum.wins    ?? d.wins   ?? 0);
  const backendLosses  = Number(analyticsSum.losses  ?? d.losses ?? 0);
  const backendWinrate = Number(
    analyticsSum.win_rate ??
    ((backendWins + backendLosses) ? (backendWins / (backendWins + backendLosses) * 100) : 0)
  );

  const displayTotal   = backendTotal;
  const displayWins    = backendWins;
  const displayLosses  = backendLosses;
  const displayWinrate = backendWinrate;
  const displayCount   = Number(analyticsSum.trades_closed) || history.length;
  const netTotal       = backendNet;

  const drift = Number(analyticsSum.drift ?? 0);
  const grossAbs = Math.abs(backendTotal);
  const driftThreshold = Math.max(5.0, grossAbs * 0.005);
  const driftOk = Math.abs(drift) <= driftThreshold;

  const lb = $('history-lookback-chip');
  if(lb){
    const days = Number(d.lookback_days || (CFG && CFG.history_lookback_days) || 89);
    lb.textContent = `${days}d window`;
    lb.title = `History window: ${days} days`;
  }
  const rc = $('history-records-chip');
  if(rc){
    const recs = Number(d.total_records_fetched || 0);
    if(recs > 0){
      rc.textContent = `${recs} records`;
      rc.title = `Raw income records across ${d.lookback_days || 89}d`;
      rc.classList.remove('hidden');
    } else {
      rc.classList.add('hidden');
    }
  }

  $('history-count').innerText = displayCount;
  const totalEl = $('total-pnl'); const chip = $('history-total-pnl-chip');
  const txt = (displayTotal>=0?'+':'') + '$' + displayTotal.toFixed(2);
  if(totalEl){ totalEl.innerText = txt; totalEl.style.color = displayTotal>=0?'var(--green)':'var(--red)'; }
  if(chip){
    chip.innerText = 'Gross ' + txt;
    chip.style.color = displayTotal>=0?'var(--green)':'var(--red)';
    chip.style.background = displayTotal>=0?'var(--green-soft)':'var(--red-soft)';
    chip.style.borderColor = displayTotal>=0?'var(--green-line)':'var(--red-line)';
  }

  $('wins').innerText = displayWins;
  $('losses').innerText = displayLosses;
  $('total-trades').innerText = displayCount + ' trades' + (driftOk ? ' • verified' : '');
  $('winrate').innerText = displayWinrate.toFixed(1) + '%';
  const ratioFill = $('ratio-bar-fill');
  if(ratioFill){
    ratioFill.style.width = Math.min(100, displayWinrate) + '%';
    ratioFill.style.background = displayWinrate >= 50 ? 'var(--green)' : 'var(--red)';
  }

  const netTxt = (netTotal>=0?'+':'') + '$' + netTotal.toFixed(2);
  const netEl = $('net-pnl');
  if(netEl){ netEl.innerText = netTxt; netEl.style.color = netTotal>=0?'var(--green)':'var(--red)'; }

  const feesTotal = -(backendComm + totalFunding);
  if($('total-fees')) $('total-fees').innerText = '$' + feesTotal.toFixed(2);
  if($('fees-breakdown')){
    const commAbs = Math.abs(backendComm).toFixed(2);
    const fundSigned = (totalFunding >= 0 ? '+' : '-') + '$' + Math.abs(totalFunding).toFixed(2);
    $('fees-breakdown').innerText = `comm $${commAbs} • fund ${fundSigned}`;
  }

  const vb = $('verify-badge'); const vt = $('history-verify-text'); const ci = $('history-calc-info');
  /* FIX #1: toggle .hidden class alongside innerHTML */
  if(history.length){
    if(driftOk){
      if(vb){ vb.classList.remove('hidden'); vb.innerHTML = '<span class="verify-ok">✓ VERIFIED</span>'; }
      if(vt) vt.innerHTML = `<span class="verify-ok">Σ verified (drift $${drift.toFixed(2)})</span>`;
      if(ci) ci.classList.add('hidden');
    } else {
      if(vb){ vb.classList.remove('hidden'); vb.innerHTML = '<span class="verify-warn">⚠ CHECK</span>'; }
      if(vt) vt.innerHTML = `<span class="verify-warn">Drift $${drift.toFixed(2)} — analytics totals</span>`;
      if(ci){
        ci.classList.remove('hidden');
        const tradesNet = fromTrades.net_pnl || 0;
        const incomeNet = fromIncome.net_pnl || 0;
        ci.innerText = `trades: $${tradesNet.toFixed(2)} / income: $${incomeNet.toFixed(2)}`;
      }
    }
  } else {
    if(vb){ vb.innerHTML = ''; vb.classList.add('hidden'); }
    if(vt) vt.innerHTML = '';
    if(ci) ci.classList.add('hidden');
  }

  const foot = $('pnl-foot');
  if(history.length){
    body.dataset.loaded = '1';
    let running = 0;
    let rowsHtml = history.map(h => {
      const pnl  = Number(h.pnl) || 0;
      const comm = Number(h.commission) || 0;
      const net  = Number(h.net !== undefined ? h.net : (pnl + comm));

      running += net;

      const isWin = net >= 0;

      const pnlAbs = `$${Math.abs(pnl).toFixed(2)}`;
      const pnlDisplay = pnl >= 0 ? `+${pnlAbs}` : `-${pnlAbs}`;
      const commDisplay = `-${'$' + Math.abs(comm).toFixed(2)}`;
      const netDisplay = net >= 0 ? `+${'$' + Math.abs(net).toFixed(2)}` : `$${net.toFixed(2)}`;
      const runDisplay = running >= 0 ? `+${'$' + Math.abs(running).toFixed(2)}` : `$${running.toFixed(2)}`;

      return `<tr data-sym="${esc((h.symbol||'').toUpperCase())}">
        <td><div class="cell-inner left"><span class="val dim-val">${esc(h.time||'')}</span></div></td>
        <td><div class="cell-inner left"><span class="val sym-val">${esc(h.symbol||'')}</span></div></td>
        <td><div class="cell-inner center"><span class="badge ${isWin?'badge-win':'badge-loss'}">${isWin?'WIN':'LOSS'}</span></div></td>
        <td><div class="cell-inner right"><span class="val ${pnl>=0?'pnl-pos':'pnl-neg'}">${pnlDisplay}</span></div></td>
        <td><div class="cell-inner right"><span class="val" style="color:var(--ink-faint)">${commDisplay}</span></div></td>
        <td><div class="cell-inner right"><span class="val ${net>=0?'pnl-pos':'pnl-neg'}">${netDisplay}</span></div></td>
        <td><div class="cell-inner center"><span class="trade-status ${isWin?'win':'loss'}">CLOSED</span></div></td>
        <td><div class="cell-inner right"><span class="val ${running>=0?'pnl-pos':'pnl-neg'}">${runDisplay}</span></div></td>
      </tr>`;
    }).join('');

    if(Math.abs(totalFunding) > 0.004){
      running += totalFunding;
      rowsHtml += `<tr class="funding-row">
        <td><div class="cell-inner left"><span class="val" style="color:var(--ink-dim)">— Funding Fees —</span></div></td>
        <td><div class="cell-inner left"><span class="val faint-val">open positions</span></div></td>
        <td><div class="cell-inner center"><span class="trade-status plain">ADJUST</span></div></td>
        <td><div class="cell-inner right"><span class="val faint-val">—</span></div></td>
        <td><div class="cell-inner right"><span class="val faint-val">—</span></div></td>
        <td><div class="cell-inner right"><span class="val" style="color:${totalFunding>=0?'var(--green)':'var(--red)'}">${totalFunding>=0?'+':''}$${totalFunding.toFixed(2)}</span></div></td>
        <td><div class="cell-inner center"><span class="trade-status plain">—</span></div></td>
        <td><div class="cell-inner right"><span class="val ${running>=0?'pnl-pos':'pnl-neg'}">${running>=0?'+':''}$${running.toFixed(2)}</span></div></td>
      </tr>`;
    }

    body.innerHTML = rowsHtml;
    applyFilter('pnl-body','history-filter');

    if(foot){
      foot.classList.remove('hidden');
      $('foot-pnl').innerText = txt;
      $('foot-pnl').style.color = displayTotal >= 0 ? 'var(--green)' : 'var(--red)';
      $('foot-comm').innerText = '-$' + Math.abs(backendComm).toFixed(2);
      $('foot-net').innerText = netTxt;
      $('foot-net').style.color = netTotal >= 0 ? 'var(--green)' : 'var(--red)';
      $('foot-count').innerText = displayCount + ' trades';
    }
  } else {
    body.dataset.loaded = '1';
    /* FIX #2: rich empty state */
    body.innerHTML = emptyRow(
      8,
      'No closed trades yet',
      'History will populate after the first trade closes.'
    );
    if(foot) foot.classList.add('hidden');
  }

  if($('history-sync')) $('history-sync').innerText = 'updated ' + nowTime();
 }catch(e){ console.warn('history',e) }
}

/* ══════════════════════════════════════════════════════════════
   FILLS
   ══════════════════════════════════════════════════════════════ */
async function fetchFills(){
 try{
  const body=$('fills-body');
  const r=await fetch('/api/history/detailed'); const d=await r.json();
  const source = d.source || 'legacy';

  if(source !== 'analytics'){
    if(body) body.innerHTML = skeletonRows(8, 6);
    if($('fills-count-tab')) $('fills-count-tab').innerText = '…';
    if($('fills-total-count')) $('fills-total-count').innerText = 'warming up…';
    if($('fills-sync')) $('fills-sync').innerText = 'analytics warming up…';
    if($('fills-total-pnl')){
      $('fills-total-pnl').innerText = '—';
      $('fills-total-pnl').style.color = 'var(--ink-faint)';
      $('fills-total-pnl').style.background = 'rgba(255,255,255,.03)';
      $('fills-total-pnl').style.borderColor = 'var(--line)';
    }
    if($('fills-grouped-info')) $('fills-grouped-info').classList.add('hidden');
    return;
  }

  const trades = Array.isArray(d.trades) ? d.trades : [];

  $('fills-count-tab').innerText = trades.length;
  $('fills-total-count').innerText = (d.total_count ?? trades.length) + ' fills';
  const tpnl = Number(d.total_pnl||0);
  const pnlChip = $('fills-total-pnl');
  if(pnlChip){
    pnlChip.innerText=(tpnl>=0?'+':'')+'$'+tpnl.toFixed(2);
    pnlChip.style.color=tpnl>=0?'var(--green)':'var(--red)';
    pnlChip.style.background=tpnl>=0?'var(--green-soft)':'var(--red-soft)';
    pnlChip.style.borderColor=tpnl>=0?'var(--green-line)':'var(--red-line)';
  }

  const g = $('fills-grouped-info');
  if(g){
    g.textContent = 'raw';
    g.title = 'Raw fills — one row per execution (no grouping)';
    g.classList.remove('hidden');
  }

  if(d.error && !trades.length){
    body.dataset.loaded='1';
    body.innerHTML=emptyRow(8, d.error, 'Execution history pulls from Binance Futures API.');
    if($('fills-sync')) $('fills-sync').innerText='raw · updated '+nowTime();
    return;
  }
  if(trades.length){
    body.dataset.loaded='1';
    body.innerHTML = trades.map(t=>{
      const isRaw = (t.datetime_utc !== undefined);
      const pnl = Number(t.realizedPnl||0);
      const comm = Number(t.commission||0);
      const isBuy = (t.side||'').toUpperCase()==='BUY';
      const count = Number(t.count) || 1;
      const idRaw = t.id !== undefined ? t.id : '';

      const tidDisplay = (!isRaw && count > 1) ? `${esc(idRaw)} ×${count}` : esc(idRaw);
      const tidTitle = (!isRaw && count > 1)
        ? `Trade ID ${esc(idRaw)} — ${count} same-second fills grouped`
        : `Trade ID ${esc(idRaw)}`;

      const timeStr = isRaw ? (t.datetime_utc || '') : (t.time ? new Date(t.time).toISOString() : '');
      const displayTime = isRaw
        ? esc(String(timeStr).replace(' UTC',''))
        : esc(String(t.time||''));

      const priceStr = Number(t.price||0).toFixed(6);

      return `<tr data-sym="${esc((t.symbol||'').toUpperCase())}">
        <td><div class="cell-inner left"><span class="val dim-val">${displayTime}</span></div></td>
        <td><div class="cell-inner left"><span class="val sym-val">${esc(t.symbol||'')}</span></div></td>
        <td><div class="cell-inner center"><span class="badge ${isBuy?'badge-buy':'badge-sell'}">${isBuy?'BUY':'SELL'}</span></div></td>
        <td><div class="cell-inner right"><span class="val">$${priceStr}</span></div></td>
        <td><div class="cell-inner right"><span class="val">${Number(t.qty||0)}</span></div></td>
        <td><div class="cell-inner right"><span class="val ${pnl>0?'pnl-pos':pnl<0?'pnl-neg':''}">${pnl!==0?(pnl>0?'+':'')+'$'+pnl.toFixed(2):'—'}</span></div></td>
        <td><div class="cell-inner right"><span class="val" style="color:var(--ink-faint)">$${Math.abs(comm).toFixed(4)}</span></div></td>
        <td><div class="cell-inner right"><span class="val faint-val" title="${tidTitle}">${tidDisplay}</span></div></td>
      </tr>`;
    }).join('');
    applyFilter('fills-body','fills-filter');
  } else {
    body.dataset.loaded='1';
    /* FIX #2: rich empty state */
    body.innerHTML = emptyRow(
      8,
      'No execution fills yet',
      'Execution history pulls from Binance Futures API.'
    );
  }
  if($('fills-sync')) $('fills-sync').innerText = `raw · updated ${nowTime()}`;
 }catch(e){ console.warn('fills',e) }
}

/* ══════════════════════════════════════════════════════════════
   UPGRADE LAYER — tick animations / sparkline
   ══════════════════════════════════════════════════════════════ */
const statPrev = {}, rowPnlPrev = {};
function setStat(id, text, numeric){
  const el = $(id); if(!el) return;
  if(el.innerText !== text){
    if(numeric !== undefined && statPrev[id] !== undefined && statPrev[id] !== numeric){
      el.classList.remove('tick-up','tick-dn'); void el.offsetWidth;
      el.classList.add(numeric > statPrev[id] ? 'tick-up' : 'tick-dn');
    }
    el.innerText = text;
  }
  if(numeric !== undefined) statPrev[id] = numeric;
}

const _status0 = fetchStatus;
fetchStatus = async function(){
  const d = await _status0();
  if(!d) return d;
  try{
    setStat('balance', fmtMoney(d.balance||0), Number(d.balance||0));
    setStat('positions', String(d.position_count||0), Number(d.position_count||0));
    (d.active_trades||[]).forEach(t=>{
      const key = (t.symbol||'').toUpperCase();
      const pnl = Number(t.pnl||0);
      if(rowPnlPrev[key] !== undefined && rowPnlPrev[key] !== pnl){
        /* FIX #4: drop CSS.escape inside quoted attr selector — symbols are [A-Z0-9] */
        const row = document.querySelector(`#trades-body tr[data-sym="${key}"]`);
        if(row){ row.classList.remove('flash-up','flash-dn'); void row.offsetWidth;
          row.classList.add(pnl >= rowPnlPrev[key] ? 'flash-up' : 'flash-dn'); }
      }
      rowPnlPrev[key] = pnl;
    });
    Object.keys(rowPnlPrev).forEach(k=>{
      if(!(d.active_trades||[]).some(t=>(t.symbol||'').toUpperCase()===k)) delete rowPnlPrev[k];
    });
  }catch(e){}
  return d;
};

let sparkData = [];
function sizeSpark(){
  const c = $('pnl-spark'); if(!c) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 240, h = 30;
  const W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(h * dpr));
  if(c.width !== W || c.height !== H){ c.width = W; c.height = H; }
}
function drawSpark(){
  const c = $('pnl-spark'); if(!c) return;
  sizeSpark();
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height;
  const dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0,0,W,H);
  if(sparkData.length < 2){
    ctx.strokeStyle='rgba(255,255,255,.08)'; ctx.setLineDash([3*dpr,4*dpr]);
    ctx.lineWidth = dpr;
    ctx.beginPath(); ctx.moveTo(0,H/2); ctx.lineTo(W,H/2); ctx.stroke(); ctx.setLineDash([]);
    return;
  }
  const min = Math.min(...sparkData), max = Math.max(...sparkData), range = (max-min) || 1;
  const up = sparkData[sparkData.length-1] >= sparkData[0];
  const col = up ? '#00d07a' : '#ff3b69';
  const px = i => 2*dpr + i*(W-4*dpr)/(sparkData.length-1);
  const py = v => H-3*dpr - ((v-min)/range)*(H-6*dpr);
  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, up ? 'rgba(0,208,122,.30)' : 'rgba(255,59,105,.30)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(px(0),H);
  sparkData.forEach((v,i)=>ctx.lineTo(px(i),py(v)));
  ctx.lineTo(px(sparkData.length-1),H); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  sparkData.forEach((v,i)=> i ? ctx.lineTo(px(i),py(v)) : ctx.moveTo(px(i),py(v)));
  ctx.strokeStyle = col; ctx.lineWidth = 1.4*dpr; ctx.lineJoin='round'; ctx.lineCap='round'; ctx.stroke();
  ctx.beginPath();
  ctx.arc(px(sparkData.length-1), py(sparkData[sparkData.length-1]), 2.2*dpr, 0, 2 * Math.PI);
  ctx.fillStyle = col; ctx.fill();
}
if($('pnl-spark')) new ResizeObserver(()=>drawSpark()).observe($('pnl-spark'));

/* FIX #5: reuse cached history data — no second fetch */
const _history0 = fetchHistory;
fetchHistory = async function(){
  await _history0();
  if(!_analyticsReady) return;
  try{
    const d = _lastHistoryData || {};
    const hist = Array.isArray(d.income_history) ? d.income_history : [];
    let run = 0;
    sparkData = hist.slice(-40).map(h=>{
      run += Number(h.net !== undefined ? h.net : (Number(h.pnl)||0) + (Number(h.commission)||0));
      return run;
    });
    drawSpark();
  }catch(e){}
};

const _scan0 = fetchScan;
fetchScan = async function(){
  await _scan0();
  document.querySelectorAll('#scan-body .adx-pill').forEach(p=>{
    p.classList.toggle('hot', Number(p.innerText) >= 40);
  });
  document.querySelectorAll('#scan-body tr[data-sym]').forEach(row=>{
    const badges = [...row.querySelectorAll('.badge')].map(b=>b.innerText.toUpperCase());
    row.classList.toggle('signal-strong', badges.some(b=>b==='BUY'||b==='SELL'));
  });
};

document.addEventListener('click', e=>{
  const el = e.target.closest('.val.faint-val[title]');
  if(!el) return;
  const txt = el.innerText.trim();
  if(!txt || txt==='—') return;
  navigator.clipboard?.writeText(txt).then(()=>toast('Trade ID copied','success')).catch(()=>{});
});

function tickClock(){
  const t = new Date().toLocaleTimeString('en-GB');
  if($('header-clock')) $('header-clock').innerText = t;
  if($('tab-clock')) $('tab-clock').innerText = t;
}
setInterval(tickClock,1000); tickClock();

document.addEventListener('keydown', e=>{
  if(e.target.matches('input,textarea,[contenteditable="true"]') || e.metaKey || e.ctrlKey || e.altKey) return;
  const tabs = ['live','scanner','history','fills','logs'];
  if(e.key >= '1' && e.key <= '5'){ setTab(tabs[Number(e.key)-1]); }
  else if(e.key.toLowerCase() === 'r'){
    const active = document.querySelector('.tab-btn.active')?.dataset.tab || 'live';
    const map = { live:fetchStatus, scanner:fetchScan, history:fetchHistory, fills:fetchFills, logs:fetchLogs };
    map[active]?.();
    toast('Refreshed','info');
  }
});

/* ══════════════════════════════════════════════════════════════
   BOOTSTRAP
   ══════════════════════════════════════════════════════════════ */
(async ()=>{
  await fetchConfig();
  fetchStatus();
  fetchScan();
  fetchLogs();
  fetchHistory();
  fetchFills();

  setInterval(fetchStatus, 2500);
  setInterval(fetchScan, 4000);
  setInterval(fetchLogs, 3000);
  setInterval(fetchHistory, 8000);
  setInterval(fetchFills, 20000);
})();

/* ══════════════════════════════════════════════════════════════
   MANUAL CLOSE — REV 1.3.15
   ══════════════════════════════════════════════════════════════ */
(function(){
  const closingNow = new Set();
  const LIVE_TBODY = 'trades-body';

  function closePosition(symbol, btn){
    const sym = String(symbol || '').trim().toUpperCase();
    if(!sym || closingNow.has(sym)) return;

    const ok = window.confirm(
      'Close ' + sym + ' at MARKET?\n\n' +
      'This sends a reduce-only market order to Binance Futures ' +
      'and cannot be undone.'
    );
    if(!ok) return;

    closingNow.add(sym);
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'closing…';

    fetch('/api/close_position/' + encodeURIComponent(sym), {
      method: 'POST',
      headers: {
        'Authorization': TOKEN ? ('Bearer ' + TOKEN) : '',
        'Content-Type': 'application/json'
      }
    })
    .then(r => r.json().catch(()=>({})).then(d => {
      if(!r.ok || !d.success) throw new Error(d.error || ('HTTP ' + r.status));
      return d;
    }))
    .then(d => {
      toast(
        '✅ ' + sym + ' closed' +
        (d.order_id ? ' (#' + d.order_id + ')' : '') +
        (d.settled === false ? ' — verify!' : ''),
        'success'
      );
      setTimeout(()=>{
        closingNow.delete(sym);
        const r = $('live-refresh');
        if(r) r.click();
      }, 1500);
    })
    .catch(e => {
      toast('❌ ' + sym + ': ' + (e.message || e), 'error');
      btn.disabled = false;
      btn.textContent = original;
      closingNow.delete(sym);
    });
  }

  function injectRowButtons(){
    const tbody = $(LIVE_TBODY);
    if(!tbody) return;
    const rows = tbody.children;
    for(let i = 0; i < rows.length; i++){
      const tr = rows[i];
      if(!tr || tr.classList.contains('empty-row')) continue;
      if(tr.dataset.closeInjected === '1') continue;

      const firstTd = tr.querySelector('td');
      if(!firstTd) continue;

      let sym = (firstTd.textContent || '').trim().split(/\s+/)[0];
      if(!sym) continue;
      sym = sym.replace(/[^A-Za-z0-9]/g,'').toUpperCase();
      if(!sym || sym.length > 20) continue;

      const td = document.createElement('td');
      const inner = document.createElement('div');
      inner.className = 'cell-inner center';

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-close-position';
      btn.textContent = 'Close';
      btn.title = 'Close ' + sym + ' at market';
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        closePosition(sym, btn);
      });

      inner.appendChild(btn);
      td.appendChild(inner);
      tr.appendChild(td);
      tr.dataset.closeInjected = '1';
    }
  }

  function mount(){
    const tbody = $(LIVE_TBODY);
    if(!tbody) return false;
    injectRowButtons();
    const mo = new MutationObserver(()=>{
      window.requestAnimationFrame(injectRowButtons);
    });
    mo.observe(tbody, { childList: true, subtree: false });
    return true;
  }

  (function waitForMount(){
    let attempts = 0;
    const MAX = 30;
    const tick = ()=>{
      attempts++;
      if(mount()) return;
      if(attempts >= MAX){
        console.warn('[close] trades-body not found after ' + MAX + ' attempts');
        return;
      }
      setTimeout(tick, 500);
    };
    tick();
  })();

  const liveTabBtn = $('tabbtn-live');
  if(liveTabBtn) liveTabBtn.addEventListener('click', ()=> setTimeout(injectRowButtons, 60));
})();

/* ══════════════════════════════════════════════════════════════
   TIME EXIT CONTROL — REV 1.4.1
   Toggle TIME_EXIT on/off at runtime via /api/time_exit
   ══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_BASE = '/api/time_exit';

  const toggle    = $('te-toggle');
  const badge     = $('te-badge');
  const holdInput = $('te-hold-minutes');
  const saveBtn   = $('te-save-btn');
  const statusEl  = $('te-status');

  if (!toggle || !badge || !holdInput || !saveBtn) return;

  function setStatus(msg, kind) {
    statusEl.textContent = msg || '';
    statusEl.className = 'te-status' + (kind ? ' ' + kind : '');
    if (msg) {
      clearTimeout(setStatus._t);
      setStatus._t = setTimeout(() => {
        statusEl.textContent = '';
        statusEl.className = 'te-status';
      }, 2500);
    }
  }

  function applyBadge(enabled) {
    if (enabled) {
      badge.textContent = 'ON';
      badge.className = 'te-badge te-on';
    } else {
      badge.textContent = 'OFF';
      badge.className = 'te-badge te-off';
    }
  }

  function applyState(enabled, holdMinutes) {
    toggle.checked = !!enabled;
    applyBadge(!!enabled);
    if (Number.isFinite(holdMinutes) && holdMinutes >= 0) {
      holdInput.value = holdMinutes;
    }
    holdInput.disabled = !enabled;
  }

  async function loadState() {
    try {
      const res = await fetch(API_BASE, { cache: 'no-store' });
      const data = await res.json();
      if (!data || !data.success) {
        setStatus('Load failed', 'err');
        return;
      }
      applyState(data.time_exit_enabled, data.hold_minutes);
    } catch (e) {
      setStatus('Network error', 'err');
      console.error('[time_exit] loadState failed', e);
    }
  }

  async function saveState() {
    const enabled = !!toggle.checked;
    const holdRaw = holdInput.value;
    const holdMinutes = holdRaw === '' ? null : parseInt(holdRaw, 10);

    if (enabled && holdMinutes !== null && (isNaN(holdMinutes) || holdMinutes < 0)) {
      setStatus('Invalid minutes', 'err');
      return;
    }

    const body = { enabled };
    if (holdMinutes !== null && !isNaN(holdMinutes) && holdMinutes >= 0) {
      body.hold_minutes = holdMinutes;
    }

    saveBtn.disabled = true;
    setStatus('Saving…');

    try {
      const res = await fetch(API_BASE, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': TOKEN ? ('Bearer ' + TOKEN) : ''
        },
        body: JSON.stringify(body),
      });
      const data = await res.json();

      if (!res.ok || !data.success) {
        setStatus('❌ ' + (data.error || ('HTTP ' + res.status)), 'err');
        return;
      }

      applyState(data.time_exit_enabled, data.hold_minutes);
      setStatus('✅ Saved', 'ok');
      if (typeof toast === 'function') {
        toast('TIME_EXIT ' + (data.time_exit_enabled ? 'ENABLED' : 'DISABLED'), 'success');
      }
    } catch (e) {
      setStatus('Network error', 'err');
      console.error('[time_exit] saveState failed', e);
    } finally {
      saveBtn.disabled = false;
    }
  }

  toggle.addEventListener('change', () => {
    const on = toggle.checked;
    applyBadge(on);
    holdInput.disabled = !on;
  });

  saveBtn.addEventListener('click', saveState);

  holdInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveState(); }
  });

  loadState();
})();