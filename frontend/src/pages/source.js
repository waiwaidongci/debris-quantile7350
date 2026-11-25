import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {actions, states} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

const LEDGERS = [
  {kind: 'patrol', title: '岸巡台账', note: '冻结巡查的河段、计划开工与预估漂浮物量。'},
  {kind: 'completion', title: '完工台账', note: '作业结束后的实耗，撤销时只登记动作。'},
];

export async function overview() {
  const profile = state.profile;
  const [history, merges] = await Promise.all([api('/audit'), api('/merges')]);
  const current = merges.find(row => row.state === 'completed');
  const strip = current ? `<p class="hint">当前成表：${esc(current.name)} · 合表时点 ${esc(current.as_of_text)} · ${current.row_count ?? '—'} 行 · 数据 #${current.dataset_id}</p>` : '<p class="hint">尚无成表，先分台账入账再合表。</p>';
  return `<section class="summary"><h2>${esc(profile.organization)}</h2><p>${esc(profile.context)}</p><ol>${profile.scenario.map(step => `<li>${esc(step)}</li>`).join('')}</ol>${strip}<a class="button" href="#1">核对台账与合表</a></section><section><h2>最近操作</h2>${table(history.map(row => ({时间:time(row.at),账号:row.actor,操作:actions[row.action] ?? row.action,记录:row.subject})))}</section>`;
}

function ledgerSection(kind, title, note, data, canImport) {
  const importer = canImport ? `<form id="ledger-${kind}" class="inline-form"><label>批次名称<input name="name" value="${kind === 'patrol' ? '岸巡补登' : '完工补登'}" required maxlength="180"></label><label>台账 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">入账本批</button>${errorSlot}</form>` : '<p class="hint">分析员可补登台账批次。</p>';
  return `<section><h2>${esc(title)}</h2><p>${esc(note)}</p>${importer}<button type="button" class="secondary" data-example="${kind}">下载${esc(title)}样例 CSV</button><p id="example-error-${kind}" class="error" role="alert"></p><p class="hint">已入账 ${data.batches.reduce((sum, row) => sum + row.inserted_count, 0)} 条 · 最近批次 ${data.batches.length} 个</p>${table(data.records.map(row => ({
    工单: row.order_id, 版本: row.version, 动作: row.action_label,
    入账时间: row.posted_at, 生效时间: row.effective_at,
    河段: row.reach ?? '—', 计划开工: row.planned_start ?? '—',
    预估量: row.debris_m3 ?? '—', 实耗: row.duration_min ?? '—', 批次: row.batch_id,
  })))}<details><summary>批次记录</summary>${table(data.batches.map(row => ({
    批次: row.id, 名称: row.name, 接收: row.record_count,
    新入账: row.inserted_count, 重复: row.record_count - row.inserted_count,
    接收时间: time(row.accepted_at),
  })))}</details></section>`;
}

export async function source() {
  const profile = state.profile;
  const [ledgers, merges] = await Promise.all([api('/ledgers'), api('/merges')]);
  const canImport = state.account.role === 'analyst';
  const mergeForm = canImport ? `<form id="merge" class="inline-form"><label>合表时点（北京时间）<input name="as_of" type="datetime-local" required></label><label>成表名称<input name="name" placeholder="留空自动命名" maxlength="180"></label><button type="submit">按此时点合表</button>${errorSlot}</form>` : '<p class="hint">分析员可按指定时点合表。</p>';
  const resumable = row => row.state === 'failed' || row.state === 'running';
  const mergeTable = `<div class="table-wrap" tabindex="0"><table><thead><tr><th>任务</th><th>成表名称</th><th>合表时点</th><th>状态</th><th>成表版本</th><th>行数</th><th>受理快照</th><th>操作</th></tr></thead><tbody>${merges.map(row => `<tr><td>#${row.id}</td><td>${esc(row.name)}</td><td>${esc(row.as_of_text)}</td><td>${esc(states[row.state] ?? row.state)}${row.error ? `<br><span class="error">${esc(row.error)}</span>` : ''}</td><td>${row.dataset_id ? `<a href="#2">数据 #${row.dataset_id}</a>` : '—'}</td><td>${row.row_count ?? '—'}</td><td>${row.source_count} 条</td><td>${canImport && resumable(row) ? `<button type="button" class="secondary" data-resume="${row.id}">续跑</button>` : '—'}</td></tr>`).join('')}</tbody></table></div>`;
  return `<section><p>${esc(profile.input)}</p><p class="hint">岸巡与完工两条台账分开入账；同批重试不会重复入账，同单同版内容不一致会退回整批。合表只取指定时点已入账且生效的最高版本，每个任务只生成一份成表，补正后可按新时点另生成新版。</p>${mergeForm}</section><section><h2>成表版本</h2>${mergeTable || empty('尚未合表。')}</section>${LEDGERS.map(item => ledgerSection(item.kind, item.title, item.note, ledgers[item.kind], canImport)).join('')}`;
}

function toLocalStamp(value) {
  if (!value) return null;
  return value.replace('T', ' ').slice(0, 16);
}

export function bindSource(root, refresh) {
  for (const {kind} of LEDGERS) {
    bindForm(root, `#ledger-${kind}`, async form => {
      const file = form.elements.file.files[0];
      const result = await api(`/ledgers/${kind}/batches`, {name: form.elements.name.value, csv: await file.text()});
      state.status = result.created
        ? `${result.label}：接收 ${result.received} 条，新入账 ${result.inserted} 条，重复 ${result.duplicates} 条。`
        : `${result.label}：该批次已入账，未重复记账。`;
    }, refresh);
    root.querySelector(`[data-example="${kind}"]`)?.addEventListener('click', async () => {
      try {await download(`/example-ledger-${kind}.csv`, `${kind}-ledger.csv`);}
      catch (error) {root.querySelector(`#example-error-${kind}`).textContent = error.message;}
    });
  }
  bindForm(root, '#merge', async form => {
    const as_of = toLocalStamp(form.elements.as_of.value);
    if (!as_of) throw new Error('请填写合表时点。');
    const result = await api('/merges', {as_of, name: form.elements.name.value});
    state.status = result.created ? `已受理合表任务 #${result.id}。` : `复用已有合表任务 #${result.id}。`;
  }, refresh);
  root.querySelectorAll('[data-resume]').forEach(button => button.addEventListener('click', async () => {
    try {
      const result = await api(`/merges/${button.dataset.resume}/resume`, null, 'POST');
      state.status = result.dataset_id ? `续跑完成，成表版本 #${result.dataset_id}。` : '续跑完成。';
    } catch (error) {state.status = error.message;}
    await refresh();
  }));
}
