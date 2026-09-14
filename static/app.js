'use strict';
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const percent = value => `${(value * 100).toFixed(1)}%`;
let info, challenges = [], outputs = [], lastReport = null, panelToken = '';
let mode = 'auto', busy = false, stopped = false, current = -1, runConfig = {}, runTime;
let autosaveTimer;
const selectionKey = 'modeltrace.lastUpstream';
function rememberSelection(id) {
  try { localStorage.setItem(selectionKey, id); } catch { /* Server configs still persist. */ }
}

function status(text, style = '') {
  $('#status').textContent = text;
  $('#status').className = `pill ${style}`;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-Panel-Token': panelToken},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回无效响应（HTTP ${response.status}）。`); }
  if (!response.ok) {
    if (response.status === 401) $('#accessForm').hidden = false;
    throw new Error(data.error || `请求失败（HTTP ${response.status}）。`);
  }
  return data;
}

async function initialize() {
  try {
    info = await api('/api/info');
    $('#accessForm').hidden = true;
    $('#accessToken').value = '';
    $('#bank').textContent = `${info.models.length} 个候选 · ${info.responses} 条参考指纹 · ${info.revision.slice(0, 7)}`;
    $('#base').value ||= info.defaults.base_url;
    $('#model').value ||= info.defaults.model;
    $('#key').required = !info.configured_key;
    $('#key').placeholder = info.configured_key ? '留空使用服务器密钥（仅配置的上游地址）' : '仅用于本次检测';
    $('#modelBank').textContent = info.models.map(model => model.name).join(' · ');
    renderUpstreams(info.upstreams || []);
    let preferred;
    try { preferred = localStorage.getItem(selectionKey); } catch { /* Optional preference. */ }
    $('#upstreamSelect').value = (info.upstreams || []).some(item => item.id === preferred)
      ? preferred : (info.upstreams || [])[0]?.id || '';
    chooseUpstream();
    $('#protocol').onchange();
    setBusy(false);
    status('就绪', 'ok');
  } catch (error) { status(error.message, 'err'); }
}

function renderUpstreams(upstreams) {
  const select = $('#upstreamSelect');
  select.innerHTML = '<option value="">新增上游（填写后自动保存）</option>' + upstreams.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.model)} · ${escapeHtml(item.key_hint)}</option>`).join('');
  select._upstreams = upstreams;
}

function chooseUpstream() {
  clearTimeout(autosaveTimer);
  const item = ($('#upstreamSelect')._upstreams || []).find(entry => entry.id === $('#upstreamSelect').value);
  $('#deleteUpstream').hidden = !item;
  rememberSelection(item?.id || '');
  if (!item) {
    $('#upstreamName').value = ''; $('#key').value = '';
    $('#key').required = !info?.configured_key;
    $('#key').placeholder = info?.configured_key ? '留空使用服务器密钥' : '首次填写后自动加密保存';
    $('#saveState').textContent = '尚未保存。信息填完整后自动保存，检测前也会检查保存状态。';
    return;
  }
  $('#upstreamName').value = item.name;
  $('#base').value = item.base_url; $('#model').value = item.model; $('#protocol').value = item.protocol; $('#tokenField').value = item.token_field;
  $('#key').value = ''; $('#key').required = false; $('#key').placeholder = `已保存密钥（${item.key_hint}），无需重复填写`;
  $('#budget').value = item.max_tokens || 4096;
  $('#temp').value = item.temperature ?? '';
  $('#reasoningEffort').value = item.reasoning_effort || '';
  $('#protocol').onchange();
  $('#saveState').textContent = '已从服务器读取。Key 留空表示使用已保存密钥；刷新后仍可使用。';
}

async function persistUpstream() {
    const body = {id: $('#upstreamSelect').value || undefined, name: $('#upstreamName').value.trim(), base_url: $('#base').value.trim(), model: $('#model').value.trim(), api_key: $('#key').value, protocol: $('#protocol').value, token_field: $('#tokenField').value, max_tokens: Number($('#budget').value), temperature: $('#temp').value, reasoning_effort: $('#reasoningEffort').value};
    if (!body.api_key && !body.id) throw new Error('尚无已保存密钥，请首次填写 Key。');
    const result = await api('/api/upstreams', body);
    const items = ($('#upstreamSelect')._upstreams || []).filter(item => item.id !== result.upstream.id);
    items.push(result.upstream);
    renderUpstreams(items); $('#upstreamSelect').value = result.upstream.id; chooseUpstream();
    return result.upstream.id;
}

async function saveUpstream() {
  if (busy || mode !== 'auto') return;
  clearTimeout(autosaveTimer);
  setBusy(true);
  try {
    await persistUpstream();
    status('上游配置已加密保存', 'ok');
  } catch (error) {
    $('#saveState').textContent = `未保存：${error.message} 输入内容已保留。`;
    status(error.message, 'err');
  } finally { setBusy(false); }
}

async function deleteUpstream() {
  const id = $('#upstreamSelect').value; if (!id || !confirm('删除这个已保存的上游配置？')) return;
  try { await fetch(`/api/upstreams/${encodeURIComponent(id)}`, {method: 'DELETE', headers: {'X-Panel-Token': panelToken}}); const list = await api('/api/upstreams'); renderUpstreams(list.upstreams); chooseUpstream(); status('配置已删除', 'ok'); }
  catch (error) { status(error.message, 'err'); }
}

function setBusy(value) {
  busy = value;
  $('#apiFields').disabled = value || mode === 'manual';
  $('#start').disabled = value || !info;
  $('#autoTab').disabled = value;
  $('#manualTab').disabled = value;
  $('#manualAnalyze').disabled = value;
  $('#saveUpstream').disabled = value;
  $('#saveUpstream').hidden = mode === 'manual';
  $('#stop').hidden = !value || mode !== 'auto';
  document.querySelectorAll('[data-output]').forEach(input => { input.disabled = value; });
}

function switchMode(nextMode) {
  if (busy) return;
  mode = nextMode;
  challenges = []; outputs = []; lastReport = null;
  $('#report').hidden = true;
  $('#apiFields').hidden = mode === 'manual';
  $('#manualIntro').hidden = mode !== 'manual';
  $('#manualAnalyze').hidden = true;
  $('#configTitle').textContent = mode === 'auto' ? '连接上游' : '准备手动检测';
  $('#start').textContent = mode === 'auto' ? '开始检测 · 3 次请求 →' : '生成三条挑战 →';
  $('#runHint').textContent = mode === 'auto'
    ? '上游配置自动加密保存。点击开始最多调用 3 次，不自动重试；检测失败也会保留配置。'
    : '手动粘贴不会从本面板调用上游。至少一份有效回答即可归因，建议收集完整三份。';
  $('#autoTab').setAttribute('aria-pressed', String(mode === 'auto'));
  $('#manualTab').setAttribute('aria-pressed', String(mode === 'manual'));
  setBusy(false); render(); status('就绪', 'ok');
}

function render() {
  const list = $('#challengeList');
  $('#counter').textContent = `${outputs.filter(Boolean).length} / 3 已完成`;
  if (!challenges.length) {
    list.innerHTML = '<div class="empty">等待开始检测<br><small>三条挑战将独立处理，逐条显示进度。</small></div>';
    return;
  }
  list.innerHTML = challenges.map((challenge, index) => {
    const output = outputs[index];
    const label = current === index ? '正在请求上游…' : output ? (output.accepted ? '已计入' : '未计入') : '等待处理';
    return `<article class="challenge ${output?.accepted ? 'done' : ''}">
      <div><b>挑战 ${index + 1}</b><small>目标 ${challenge.expected_count} 个整数${output ? ` · 解析 ${output.parsed_numbers || 0} 个` : ''}</small></div>
      <span class="challenge-status ${current === index ? 'working' : ''}">${label}</span>
      ${mode === 'manual' ? `<button type="button" class="copy" data-copy="${index}">复制提示词</button>` : ''}
      <details><summary>查看提示词${mode === 'auto' && output?.text ? ' / 输出' : ''}</summary><pre>${escapeHtml(challenge.prompt)}</pre>${mode === 'auto' && output?.text ? `<pre>${escapeHtml(output.text)}</pre>` : ''}</details>
      ${mode === 'manual' ? `<label class="paste">粘贴挑战 ${index + 1} 的完整输出<textarea data-output="${index}" spellcheck="false" placeholder="拒答、已知截断或工具生成请留空"></textarea></label>` : ''}
      ${output ? `<div class="state">${escapeHtml(output.accepted ? `有效数字已计入${output.elapsed_seconds === undefined ? '' : ` · ${output.elapsed_seconds} 秒`}` : output.rejection || output.error || '有效数字不足')}</div>` : ''}
    </article>`;
  }).join('');
  list.querySelectorAll('[data-copy]').forEach(button => {
    button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(challenges[Number(button.dataset.copy)].prompt);
        button.textContent = '已复制';
      } catch { status('复制不可用，请展开提示词后手动选中复制。', 'err'); }
    };
  });
}

async function start(event) {
  event.preventDefault();
  if (busy) return;
  const config = mode === 'auto' ? {
    base_url: $('#base').value.trim(), model: $('#model').value.trim(),
    protocol: $('#protocol').value, max_tokens: Number($('#budget').value),
    temperature: $('#temp').value, token_field: $('#tokenField').value, reasoning_effort: $('#reasoningEffort').value,
  } : {mode: 'manual'};
  let key = mode === 'auto' ? $('#key').value : '';
  let upstreamId = mode === 'auto' ? $('#upstreamSelect').value : '';
  clearTimeout(autosaveTimer);
  setBusy(true); stopped = false; $('#stop').disabled = false;
  try {
    if (mode === 'auto' && (key || upstreamId)) {
      $('#saveState').textContent = '正在保存配置…';
      upstreamId = await persistUpstream();
      key = ''; // Saved credentials are subsequently resolved by the server.
    }
    const result = await api('/api/challenges', {});
    challenges = result.challenges; outputs = []; lastReport = null; current = -1;
    runTime = new Date().toISOString(); runConfig = config;
    $('#report').hidden = true;
    render();
    if (mode === 'manual') {
      $('#manualAnalyze').hidden = false;
      status('挑战已生成，请分别收集完整输出。', 'ok');
      return;
    }
    $('#key').value = '';
    for (let index = 0; index < challenges.length && !stopped; index++) {
      current = index; render(); status(`正在执行挑战 ${index + 1} / 3，最长等待 240 秒`);
      try {
        outputs[index] = await api('/api/probe', {...config, api_key: key, upstream_id: upstreamId, token: challenges[index].token});
      } catch (error) {
        outputs[index] = {accepted: false, error: error.message, parsed_numbers: 0};
        stopped = true;
        status(`挑战 ${index + 1} 失败，后续请求已停止。`, 'err');
      }
    }
    current = -1; render();
    if (outputs.some(output => output?.accepted)) await analyzeAutomatic();
    else status(outputs.find(output => output?.error)?.error || '本轮没有有效回答；请查看挑战详情。', 'err');
  } catch (error) { status(error.message, 'err'); }
  finally { key = ''; current = -1; setBusy(false); }
}

async function analyzeAutomatic() {
  const items = outputs.map((output, index) => output?.accepted ? {token: challenges[index].token, text: output.text} : null).filter(Boolean);
  const result = await api('/api/analyze', {outputs: items});
  showReport(result);
}

async function analyzeManual() {
  if (busy) return;
  const texts = challenges.map((_, index) => document.querySelector(`[data-output="${index}"]`).value);
  $('#report').hidden = true; lastReport = null;
  setBusy(true);
  try {
    const result = await api('/api/analyze', {outputs: texts.map((text, index) => ({token: challenges[index].token, text}))});
    outputs = result.diagnostics.map((diagnostic, index) => ({...diagnostic, text: texts[index], rejection: diagnostic.accepted ? null : '有效数字不足或未填写'}));
    render();
    document.querySelectorAll('[data-output]').forEach(input => { input.value = texts[Number(input.dataset.output)]; });
    showReport(result);
  } catch (error) { status(error.message, 'err'); }
  finally { setBusy(false); }
}

function showReport(result) {
  lastReport = {
    created_at: runTime, mode, requested: runConfig,
    source: 'https://github.com/xqy2006/ModelTrace', result,
    challenges: challenges.map((challenge, index) => ({id: challenge.id, prompt: challenge.prompt, expected_count: challenge.expected_count, response: outputs[index] || null})),
    interpretation: '闭集归因，仅代表在当前候选库中的相对概率，不能证明模型身份或量化降智。',
  };
  $('#report').hidden = false;
  $('#requestedEffort').textContent = mode === 'auto'
    ? `请求的推理档位：${runConfig.reasoning_effort || '上游默认（未传参）'}。此项记录请求设置，不证明上游实际执行。`
    : '手动模式无法获知模型使用的推理档位。';
  $('#partialNote').hidden = result.used_outputs === 3;
  $('#partialNote').textContent = `仅 ${result.used_outputs} / 3 份有效回答，当前为不完整检测；按原算法使用对应查询数量的校准参数。`;
  $('#summary').innerHTML = `<div class="summary"><div><small>最可能模型 / 库内候选</small><strong>${escapeHtml(result.prediction_name)}</strong></div><div><small>库内归因概率</small><strong>${percent(result.probability)}</strong></div><div><small>模型家族</small><strong>${escapeHtml(result.family_prediction_name)} · ${percent(result.family_probability)}</strong></div></div>`;
  $('#table').innerHTML = `<table><thead><tr><th>候选模型</th><th>家族</th><th>归因概率</th><th>分布相似度</th></tr></thead><tbody>${result.results.map(item => `<tr class="${item.model === result.prediction ? 'winner' : ''}"><td>${escapeHtml(item.display_name)}</td><td>${escapeHtml(item.family_name)}</td><td><progress max="1" value="${item.probability}" aria-label="${escapeHtml(item.display_name)} 归因概率"></progress> ${percent(item.probability)}</td><td>${percent(item.profile_similarity)}</td></tr>`).join('')}</tbody></table>`;
  status(result.used_outputs === 3 ? '三次挑战归因完成' : '部分回答归因完成，请查看提示', result.used_outputs === 3 ? 'ok' : '');
}

$('#download').onclick = () => {
  if (!lastReport) return;
  const link = document.createElement('a');
  const url = URL.createObjectURL(new Blob([JSON.stringify(lastReport, null, 2)], {type: 'application/json'}));
  link.href = url; link.download = `modeltrace-${Date.now()}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('#configForm').onsubmit = start;
$('#saveUpstream').onclick = saveUpstream;
$('#deleteUpstream').onclick = deleteUpstream;
$('#upstreamSelect').onchange = chooseUpstream;
$('#apiFields').addEventListener('input', event => {
  if (event.target.id === 'upstreamSelect' || busy) return;
  clearTimeout(autosaveTimer);
  $('#saveState').textContent = '有未保存修改，填写完整后自动保存。';
  autosaveTimer = setTimeout(() => {
    if (!busy && mode === 'auto' && $('#base').value && $('#model').value &&
        ($('#key').value || $('#upstreamSelect').value) && $('#configForm').checkValidity()) saveUpstream();
  }, 1200);
});
$('#manualAnalyze').onclick = analyzeManual;
$('#autoTab').onclick = () => switchMode('auto');
$('#manualTab').onclick = () => switchMode('manual');
$('#stop').onclick = () => { stopped = true; $('#stop').disabled = true; status('将等待当前请求结束，不再发送后续挑战。'); };
$('#protocol').onchange = () => {
  $('#tokenFieldLabel').hidden = $('#protocol').value !== 'openai';
  $('#temp').max = $('#protocol').value === 'anthropic' ? '1' : '2';
  const anthropic = $('#protocol').value === 'anthropic';
  $('#reasoningEffort').disabled = anthropic;
  if (anthropic) $('#reasoningEffort').value = '';
  $('#reasoningHint').textContent = anthropic
    ? 'Anthropic Messages 的 thinking 参数尚未接入，当前使用上游默认；切换到此协议会清除 OpenAI 档位。'
    : '档位依赖上游和模型支持。GPT-6 Astra 不支持 none。较高档位可能增加用量与等待时间，建议 Temperature 留空。';
};
$('#accessForm').onsubmit = event => { event.preventDefault(); panelToken = $('#accessToken').value; initialize(); };
$('#challengeList').addEventListener('input', event => {
  if (event.target.matches('[data-output]')) { $('#report').hidden = true; lastReport = null; }
});
initialize();
