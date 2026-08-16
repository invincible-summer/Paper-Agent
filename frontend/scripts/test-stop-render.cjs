// D-088: confirm the send slot renders the stop button while responding.
// Use a search topic so the turn takes a few seconds (real backend).
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (e) => console.error("PAGEERR:", e.message));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  await page.evaluate(() => { try { localStorage.removeItem("paper-agent-chat"); } catch {} });
  await page.reload({ waitUntil: "networkidle" });

  // Start a search turn.
  const ta = page.locator("textarea");
  await ta.fill("搜索图神经网络的论文");
  await ta.press("Enter");

  // Screenshot the input bar immediately — the send slot should now show a
  // Square (stop) icon, not the ArrowUp.
  await page.waitForTimeout(1500);
  const stopVisible = await page.locator('button[title="停止生成"]').isVisible().catch(() => false);
  const arrowVisible = await page.locator('button[type="button"]').filter({ has: page.locator("svg.lucide-arrow-up") }).count();
  console.log("stop visible:", stopVisible, "arrow buttons:", arrowVisible);
  await page.screenshot({ path: "/tmp/d088_render.png", fullPage: false });
  await browser.close();
  console.log(stopVisible ? "PASS" : "FAIL: stop button not rendered");
  process.exit(stopVisible ? 0 : 1);
})();
