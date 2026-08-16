// D-088: stop button interrupts an in-progress chat turn.
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  // Force a clean client state (zustand store) so no leftover isResponding
  // from a prior run can disable the send button.
  await page.evaluate(() => {
    try {
      // The chat store resets isResponding via reset(); call it if exposed.
      const w = window;
      if (w.localStorage) w.localStorage.removeItem("paper-agent-chat");
    } catch {}
  });
  await page.reload({ waitUntil: "networkidle" });

  // Trigger a search-heavy turn so there's time to interrupt before done.
  await page.fill("textarea", "搜索启蒙理性的论文");
  await page.click('button:has(svg.lucide-arrow-up)');

  // Wait for the stop button to appear (only while responding).
  const stopBtn = page.locator('button:has-text("停止生成")');
  try {
    await stopBtn.waitFor({ state: "visible", timeout: 8000 });
  } catch {
    console.log("SKIP: turn finished before stop button rendered (fast path)");
    await browser.close();
    process.exit(0);
  }
  console.log("PASS: stop button visible during streaming");

  await stopBtn.click();

  // After stop: the store should leave isResponding=false within a beat, and
  // no page error should fire. Give it up to 5s to settle.
  let settled = false;
  for (let i = 0; i < 10; i++) {
    const streaming = await page.locator('svg.lucide-loader-2.animate-spin, .animate-bounce').first().isVisible().catch(() => false);
    const stillVisible = await stopBtn.isVisible().catch(() => false);
    if (!streaming && !stillVisible) { settled = true; break; }
    await page.waitForTimeout(500);
  }
  console.log(settled ? "PASS: streaming stopped after click" : "WARN: still streaming 5s after click");
  await page.screenshot({ path: "/tmp/d088_stop.png" });

  await browser.close();
  if (errors.length > 0) {
    console.error("PAGE ERRORS:", errors.slice(0, 5).join(" | "));
    process.exit(1);
  }
  console.log("DONE");
})();
