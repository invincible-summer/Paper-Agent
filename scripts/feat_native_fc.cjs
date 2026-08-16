const { chromium } = require('playwright');

const BASE = 'http://localhost:3000';
const BACKEND = 'http://localhost:8123';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERR: ' + e.message));

  const out = (k, v) => console.log(`[${k}] ${v}`);
  let pass = 0, fail = 0;
  const check = (k, cond, detail) => {
    if (cond) { pass++; out('PASS', k + (detail ? ' :: ' + detail : '')); }
    else { fail++; out('FAIL', k + (detail ? ' :: ' + detail : '')); }
  };

  try {
    // --- 1. Structured workflow page (/) loads ---
    await page.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 30000 });
    check('page_structured_loads', await page.title().then(t => t.length > 0), 'title="' + await page.title() + '"');
    // Sidebar / appshell present
    check('sidebar_present', await page.locator('aside, nav').first().count() > 0);

    // --- 2. Navigate to chat page (/chat) ---
    await page.goto(BASE + '/chat', { waitUntil: 'networkidle', timeout: 30000 });
    check('page_chat_loads', await page.title().then(t => t.length > 0));
    // chat input present
    const inputVisible = await page.locator('textarea, input[type="text"]').first().count();
    check('chat_input_present', inputVisible > 0);

    // --- 3. Send a message and verify SSE streaming reply (native FC path) ---
    const chatInput = page.locator('textarea').first();
    await chatInput.fill('你好');
    // submit: press Enter or find a send button
    await chatInput.press('Enter').catch(async () => {
      const btn = page.getByRole('button').filter({ hasText: /send|发送|提交/i }).first();
      if (await btn.count()) await btn.click();
    });
    // wait for an assistant reply to render in chat prose
    try {
      await page.waitForSelector('.chat-prose, [class*="assistant"]', { timeout: 30000 });
      const replyText = await page.locator('.chat-prose, [class*="assistant"]').last().textContent({ timeout: 5000 }).catch(() => '');
      check('chat_reply_streamed', replyText && replyText.trim().length > 0, 'reply="' + (replyText||'').slice(0,40) + '"');
    } catch (e) {
      check('chat_reply_streamed', false, 'no reply within 30s: ' + e.message.slice(0,80));
    }

    // --- 4. History sidebar loads (REST via Next.js proxy to backend) ---
    const historyItems = await page.locator('aside [class*="history"], nav [class*="history"], [data-testid*="history"]').count().catch(() => 0);
    // Also try the history REST endpoint directly via frontend proxy
    const histApi = await page.evaluate(async () => {
      try {
        const r = await fetch('/api/v1/chat/history');
        if (!r.ok) return 'http ' + r.status;
        const j = await r.json();
        return 'ok items=' + (Array.isArray(j.items) ? j.items.length : (Array.isArray(j) ? j.length : 'n/a'));
      } catch (e) { return 'fetch err: ' + e.message.slice(0,60); }
    });
    check('history_api_reachable', String(histApi).startsWith('ok'), histApi);

    // --- 5. Settings popover (DeepRead toggle etc.) opens ---
    const settingsBtn = page.getByRole('button').filter({ hasText: /setting|设置|⚙|gear/i }).first();
    if (await settingsBtn.count()) {
      await settingsBtn.click().catch(() => {});
      await page.waitForTimeout(500);
      // look for DeepRead toggle text
      const popText = await page.locator('[role="dialog"], [class*="popover"]').textContent().catch(() => '');
      check('settings_popover_opens', popText && popText.length > 0, 'popover chars=' + (popText||'').length);
    } else {
      check('settings_popover_opens', false, 'no settings button found');
    }

    // screenshot for visual confirmation
    await page.screenshot({ path: '/tmp/feat_chat_reply.png', fullPage: true });
    out('SHOT', '/tmp/feat_chat_reply.png saved');

  } catch (e) {
    out('ERROR', e.message.slice(0, 200));
    await page.screenshot({ path: '/tmp/feat_error.png' }).catch(() => {});
  } finally {
    check('no_console_errors', errors.length === 0, errors.slice(0,3).join(' | '));
    console.log(`\n=== ${pass} passed, ${fail} failed ===`);
    await browser.close();
    process.exit(fail > 0 ? 1 : 0);
  }
})();
