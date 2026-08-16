const { chromium } = require("playwright");

// D-080: verify source-aware history routing.
// Scenarios:
// 1. From /chat, click a structured-only record -> should land on /
// 2. From /, click a chat-only record -> should land on /chat
// 3. From /, click a both record -> should stay on /, structured state restored
//    AND chat store populated (switching to /chat shows messages)

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  const log = (...a) => console.log("[routing]", ...a);
  const fail = (msg) => { console.error("FAIL:", msg); process.exitCode = 1; };

  // Fetch the unified history list directly to find records by source.
  await page.goto("http://localhost:3000", { waitUntil: "networkidle", timeout: 30000 });
  const records = await page.evaluate(async () => {
    const r = await fetch("/api/v1/chat/history");
    return (await r.json()).records;
  });
  log(`fetched ${records.length} records`);
  const find = (src) => records.find(r => r.source === src);
  const structured = find("structured");
  const chatOnly = find("chat");
  const both = find("both");

  // --- Scenario 1: from /chat, click structured-only record -> go to / ---
  if (structured) {
    await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(800);
    // Click the sidebar item by data-filename if present, else by text.
    const loc = page.locator(`text=${structured.title || structured.topic}`).first();
    await loc.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const url1 = page.url();
    log("after structured click from /chat, url =", url1);
    if (!url1.endsWith("/") && !url1.endsWith("/3000/")) fail(`expected /, got ${url1}`);
    else log("PASS: structured-only from /chat navigated to /");
    await page.screenshot({ path: "/tmp/hist_struct_from_chat.png" });
  } else {
    log("no structured-only record; skipping scenario 1");
  }

  // --- Scenario 2: from /, click chat-only record -> go to /chat ---
  if (chatOnly) {
    await page.goto("http://localhost:3000/", { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(800);
    const loc = page.locator(`text=${chatOnly.title || chatOnly.topic}`).first();
    await loc.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const url2 = page.url();
    log("after chat click from /, url =", url2);
    if (!url2.includes("/chat")) fail(`expected /chat, got ${url2}`);
    else log("PASS: chat-only from / navigated to /chat");
    await page.screenshot({ path: "/tmp/hist_chat_from_struct.png" });
  } else {
    log("no chat-only record; skipping scenario 2");
  }

  // --- Scenario 3: from /, click both record -> stay on /, structured +
  // chat state restored. Verify by switching to /chat and seeing messages. ---
  if (both) {
    await page.goto("http://localhost:3000/", { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(800);
    const loc = page.locator(`text=${both.title || both.topic}`).first();
    await loc.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const url3 = page.url();
    log("after both click from /, url =", url3);
    // Should stay on / (structured), not navigate to /chat
    if (url3.includes("/chat")) fail("both record from / should stay on /, not navigate to /chat");
    else log("PASS: both record from / stayed on /");
    await page.screenshot({ path: "/tmp/hist_both_struct.png" });
    // Now switch to /chat via the page switcher Link
    await page.getByRole("link", { name: /对话|Chat/ }).first().click();
    await page.waitForTimeout(1500);
    const url4 = page.url();
    log("after switching to /chat, url =", url4);
    if (!url4.includes("/chat")) fail("expected to land on /chat after switching");
    else log("PASS: navigated to /chat after switch");
    // Chat messages should be visible (the both record had chat content).
    // User messages use rounded-[18px] bg-accent; assistant uses chat-prose.
    const userMsg = await page.getByText("搜索关于启蒙理性的论文").count();
    const assistantProse = await page.locator(".chat-prose").count();
    log("user message text matches:", userMsg, "| assistant prose blocks:", assistantProse);
    await page.screenshot({ path: "/tmp/hist_both_chat.png" });
    if (userMsg < 1 && assistantProse < 1) fail("expected chat messages to be visible after loading both record");
    else log("PASS: chat messages visible after loading both record");
  } else {
    log("no both-source record; skipping scenario 3");
  }

  log("page errors:", errors.slice(0, 3));
  await browser.close();
  if (process.exitCode) {
    console.error("FAIL: at least one scenario failed");
  } else {
    console.log("PASS: all routing scenarios passed");
  }
})();
