// Synthetic browser fixtures only. No request is sent to a real backend.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.env.STUDIO_TEST_URL || 'http://127.0.0.1:3107';
const output = fs.mkdtempSync('/tmp/paper-studio-design-');
const stamp = { version: 1, updated_by: 'fixture', updated_at: 1788600000 };
const auth = { auth_required: true, guest_access: true, registration_open: true, email_requirement: 'none', ...stamp };
const file = { id: 'fixture-pdf', filename: '理解表征学习：固定条件下的证据与局限.pdf', ext: 'pdf', media_type: 'application/pdf', char_count: 4250, size: 182400, created_at: '2026-09-05T08:00:00Z', multimodal_status: 'pending', element_count: 0 };
const paper = { id: 'fixture-paper', title: 'Learning representations under fixed conditions: evidence, assumptions, and open questions', authors: ['Lin Chen', 'Alex Morgan'], year: 2026, venue: 'Synthetic Research Notes', source: 'Fixture', citation_count: 24, abstract: 'This synthetic record is used only to verify the interface. The evaluation separates measured improvements from assumptions about changing distributions.', keywords: ['representation'], urls: ['https://example.org/paper'], sessions: [{ filename: 'fixture.json', title: '表征学习的研究线索' }], in_review: true };
const policy = { sources: {}, capabilities: {}, search_deadline_seconds: 30, per_source_timeout_seconds: 10, routing_mode: 'smart', ...stamp };
const storage = { preset: 'balanced', session_ttl_seconds: 86400, upload_ttl_seconds: 86400, max_upload_bytes: 10485760, export_ttl_seconds: 86400, cache_ttl_seconds: 3600, trace_ttl_seconds: 3600, cleanup_interval_minutes: 30, observe_threshold_percent: 70, pressure_threshold_percent: 80, critical_threshold_percent: 90, hard_stop_threshold_percent: 98, pressure_strategy: 'continuous_evict', critical_strategy: 'pause_heavy', trace_mode: 'off', ...stamp };
const responses = {
  '/auth/config': auth,
  '/health': { api_configured: true },
  '/chat/history': { records: [{ filename: 'fixture.json', title: '表征学习的研究线索', timestamp: '20260905', paper_count: 8, has_map: true }] },
  '/chat/history/fixture.json': { messages: [{ role: 'user', content: '如何判断论文中的结论是否得到充分支持？' }, { role: 'assistant', content: '## 从证据边界开始\n\n先区分**作者的主张**与实际测量。\n\n| 问题 | 检查位置 |\n| --- | --- |\n| 数据是否匹配 | 实验设置 |\n| 有哪些限制 | 局限讨论 |\n\n损失函数 $L = \\sum_i (y_i-\\hat y_i)^2$ 需要结合实验条件理解。' }], session: { session_id: 'fixture-session', topic: '表征学习', attachments: [file] } },
  '/chat/files': { files: Array.from({ length: 12 }, (_, i) => ({ ...file, id: `fixture-${i}`, filename: i ? `研究笔记 ${i} — 实验设置与证据边界.pdf` : file.filename })) },
  '/papers': { papers: [paper, { ...paper, id: 'second', title: 'Beyond a single score: evaluating the limits of generalization', in_review: false }] },
  '/usage-document': { title: '使用指南', content: '# 从问题开始\n\n围绕研究问题组织文献与讨论。\n\n## 阅读原文\n\n上传 PDF 后，在阅研中划选文字，记录自己的判断。\n\n## 整理发现\n\n把发现带回对话，继续比较和写作。', ...stamp },
  '/admin/agent-keys': { items: [] },
  '/admin/accounts/usage': { items: [], totals: {} },
  '/admin/feedback': { items: [], total: 0, counts: { open: 0, resolved: 0 } },
  '/admin/auth-settings': { settings: auth, smtp: { configured: false, sender: '', from_name: '' } },
  '/admin/display-policy': { policy: { tool_cards_enabled: true, tool_error_cards_enabled: true, skill_card_enabled: false, research_map_svg_enabled: true, research_map_mermaid_enabled: false, research_map_html_enabled: false, research_map_markdown_enabled: true, bibtex_export_mode: 'bib_and_md', ...stamp } },
  '/admin/paper-search/policy': { policy, defaults: policy, capabilities: {}, quick_preset: {}, source_catalog: [], configuration_status: {}, disabled_by: {}, disabled_reason: {}, diagnostic_summary: null, last_checked_at: null },
  '/admin/paper-search/diagnostics/latest': { capability: null, recent: [] },
  '/admin/performance-policy': { settings: { startup_prewarm_mode: 'blocking', map_citation_mode: 'fast', ...stamp }, defaults: { startup_prewarm_mode: 'blocking', map_citation_mode: 'fast' }, openalex_enabled: true, effective_map_citation_mode: 'fast', map_citation_disabled_reason: '', prewarm: { active_mode: 'blocking', prewarm_state: 'ready', duration_ms: 120, last_error: '' }, restart_required: false },
  '/admin/tool-budgets': { policy: { budgets: {}, default_budget_seconds: 30, reserve_seconds: 8, api_turn_soft_seconds: 95, api_turn_hard_seconds: 105, ...stamp }, catalog: [], limits: { min_seconds: 1, max_seconds: 300, min_reserve: 1, max_reserve: 30, gateway_timeout_seconds: 120, min_api_turn_soft_seconds: 10, api_turn_soft_seconds: 95, min_api_turn_hard_seconds: 15, api_turn_hard_seconds: 105, web_turn_soft_seconds: 240, web_turn_hard_seconds: 300 }, breaker: { threshold: 3, cooldown_seconds: 300 }, breaker_states: {} },
  '/admin/api-storage/policy': { policy: storage, help: { items: {} } },
  '/admin/api-storage/usage': { categories: [{ category: 'uploads', status: 'normal', count: 0, bytes: 0 }], total_bytes: 0 },
  '/admin/api-storage/status': { text_chat_allowed: true, heavy_writes_paused: false, pause_reason: null, disk_percent: 20, last_cleanup_at: null },
  '/admin/api-storage/cleanup-runs': { items: [] },
};
(async () => {
  const browser = await chromium.launch({ headless: true });
  const errors = [], unknown = [];
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem('pa_auth', JSON.stringify({ token: 'synthetic-test-token', user: { id: 'fixture', username: 'fixture', display_name: '演示管理员', role: 'administrator' } })));
  let mode = 'normal', holdStream = false, releaseStream;
  const sent = [];
  await context.route('**/api/v1/**', async route => {
    const key = new URL(route.request().url()).pathname.replace('/api/v1', '');
    if (key === '/chat/stream') {
      sent.push(route.request().postDataJSON());
      if (holdStream) await new Promise(resolve => { releaseStream = resolve; });
      return route.fulfill({ contentType: 'text/event-stream', body: [
        { type: 'thinking', content: '先检查实验条件。' },
        { type: 'answer', content: '这是一条合成回答，公式 $x^2$ 已排版。' },
        { type: 'done' },
      ].map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('') }).catch(() => {});
    }
    if (/^\/chat\/file\/fixture-\d+\/raw$/.test(key)) return route.fulfill({ contentType: 'application/pdf', headers: { 'Content-Disposition': 'attachment; filename="fixture.pdf"' }, body: '%PDF synthetic download fixture' });
    if (/^\/chat\/file\/fixture-\d+$/.test(key)) return route.fulfill({ json: { filename: file.filename, text: '合成文档提取文本，用于验证预览。', char_count: 20 } });
    let body = responses[key];
    if (mode === 'error' && ['/papers', '/chat/files'].includes(key)) return route.fulfill({ status: 503, json: { detail: '测试错误' } });
    if (mode === 'empty' && key === '/papers') body = { papers: [] };
    if (mode === 'empty' && key === '/chat/files') body = { files: [] };
    if (key === '/feedback' && route.request().method() === 'POST') body = { id: 'feedback-fixture', status: 'open' };
    if (body === undefined) { unknown.push(key); return route.fulfill({ status: 404, json: { detail: 'Unmocked fixture' } }); }
    await route.fulfill({ json: body });
  });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && /hydration|did not match/i.test(m.text())) errors.push(m.text()); });
  const visit = async (url, width, name) => {
    await page.setViewportSize({ width, height: 960 });
    await page.goto(base + url, { waitUntil: 'networkidle', timeout: 90000 });
    await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert.equal(overflow, false, `${url} page overflow at ${width}`);
  };
  for (const width of [1440, 1280, 1024, 768, 390]) {
    await visit('/chat', width, 'chat');
    assert.equal(await page.locator('.history-panel').count(), width >= 1024 ? 1 : 0);
    assert.equal(await page.locator('.resource-panel').count(), width >= 1280 ? 1 : 0);
    assert.ok((await page.locator('.studio-main').boundingBox()).width >= Math.min(width, 600));
  }
  await page.getByRole('button', { name: '打开工作台空间' }).click();
  await page.getByRole('dialog', { name: '当前会话资料' }).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('button', { name: '打开工作台空间' }).evaluate(e => e === document.activeElement), true);
  await page.getByRole('button', { name: '展开左边栏' }).click();
  await page.getByRole('dialog', { name: '会话历史' }).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '展开导航', exact: true }).click();
  await page.getByRole('dialog', { name: '主导航' }).waitFor();
  await page.getByRole('button', { name: '阅研', exact: true }).click();
  await page.waitForURL('**/reader');
  assert.equal(await page.getByRole('dialog').count(), 0);
  for (const url of ['/reader', '/files?tab=papers', '/files?tab=uploads', '/usage-doc', '/feedback']) {
    for (const width of [1440, 390]) await visit(url, width, url.replace(/[^a-z]/g, '-'));
  }
  for (const admin of ['auth-settings', 'agent-keys', 'accounts-data', 'display-policy', 'paper-search', 'performance', 'api-storage', 'feedback']) {
    for (const width of [1440, 390]) await visit(`/admin/${admin}`, width, `admin-${admin}`);
    assert.ok(await page.locator('h1').count(), `admin ${admin} rendered its form`);
  }
  await visit('/admin/agent-keys', 1440, 'admin-help');
  await page.getByRole('button', { name: '创建密钥说明' }).click();
  await page.getByRole('dialog').waitFor();
  await page.keyboard.press('Tab');
  assert.equal(await page.getByRole('dialog').evaluate(e => e.contains(document.activeElement)), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('button', { name: '创建密钥说明' }).evaluate(e => e === document.activeElement), true);
  await visit('/admin/auth-settings', 1440, 'admin-confirm');
  await page.getByRole('switch').first().click();
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '关闭账号登录（高危）' });
  await confirmation.waitFor();
  const dangerButton = confirmation.locator('.btn-danger');
  assert.equal(await dangerButton.isDisabled(), true);
  await confirmation.getByRole('textbox').fill('确认');
  assert.equal(await dangerButton.isEnabled(), true);
  await confirmation.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await page.getByRole('dialog').count(), 0);
  for (mode of ['empty', 'error']) {
    await visit('/reader', 390, `reader-${mode}`);
    await visit('/files', 390, `files-${mode}`);
    if (mode === 'error') assert.ok(await page.getByRole('alert').count());
  }
  mode = 'normal';
  await visit('/chat?history=fixture.json', 1440, 'chat-messages');
  await page.locator('.chat-prose .katex').first().waitFor();
  await page.screenshot({ path: `${output}/chat-messages-1440.png` });
  await page.getByPlaceholder('输入你的问题...').fill('验证发送');
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByText('这是一条合成回答，公式', { exact: false }).waitFor();
  await page.getByTitle('重新生成回答').click();
  await page.getByRole('button', { name: '发送消息' }).waitFor();
  assert.equal(sent.at(-1).regenerate, true);
  holdStream = true;
  await page.getByPlaceholder('输入你的问题...').fill('验证停止');
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByRole('button', { name: '停止生成' }).waitFor();
  const streamDeadline = Date.now() + 5000;
  while (!releaseStream && Date.now() < streamDeadline) await page.waitForTimeout(20);
  assert.ok(releaseStream, `pending stream was intercepted; requests=${JSON.stringify(sent)}`);
  await page.getByRole('button', { name: '停止生成' }).click();
  await page.getByRole('button', { name: '发送消息' }).waitFor();
  releaseStream(); holdStream = false;
  await visit('/files?tab=uploads', 390, 'files-actions');
  await page.getByTitle('预览提取文本').first().click();
  await page.getByText('合成文档提取文本，用于验证预览。').waitFor();
  const download = page.waitForEvent('download');
  await page.getByTitle('下载原件').first().click();
  assert.ok((await download).suggestedFilename().endsWith('.pdf'));
  await visit('/feedback', 390, 'feedback-form');
  await page.locator('#feedback-content').fill('合成界面反馈：研究工作台的布局清晰。');
  await page.getByRole('button', { name: '提交反馈' }).click();
  await page.getByText('感谢你的反馈！').waitFor();
  await visit('/chat', 1440, 'settings');
  await page.getByRole('button', { name: /Settings|设置/ }).click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('button', { name: 'English', exact: true }).click();
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('dialog').count(), 0);
  // Logged-out login screen, isolated from the admin context.
  await context.addInitScript(() => localStorage.removeItem('pa_auth'));
  for (const width of [1440, 390]) await visit('/login', width, 'login');
  const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await touch.route('**/api/v1/**', route => route.fulfill({ json: responses[new URL(route.request().url()).pathname.replace('/api/v1', '')] || {} }));
  const mobile = await touch.newPage();
  await mobile.goto(base + '/chat', { waitUntil: 'networkidle' });
  const targets = await mobile.locator('.studio-toolbar button').evaluateAll(items => items.filter(e => e.getClientRects().length).map(e => ({ width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height })));
  assert.ok(targets.every(box => box.width >= 44 && box.height >= 44), 'touch toolbar targets are at least 44px');
  await mobile.screenshot({ path: `${output}/chat-touch-390.png` });
  await touch.close();
  assert.deepEqual(errors, [], 'runtime and hydration errors');
  assert.deepEqual([...new Set(unknown)], [], 'all API requests must be mocked');
  await browser.close();
  console.log(`PASS: responsive routes, drawers, focus, normal/empty/error fixtures. Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exit(1); });
