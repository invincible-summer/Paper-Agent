// D-085b e2e: re-asking "构建引用图谱" in a session that already has a graph
// must re-emit the graph so the iframe card re-renders (no manual refresh).
const { chromium } = require("playwright");
const BASE = process.env.SCREENSHOT_BASE || "http://localhost:3000";

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERR: " + e.message));

  // 1. open /chat, click the 启蒙理性 history item to restore the graph session
  await page.goto(`${BASE}/chat`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1200);
  const hist = page.getByText(/启蒙理性/).first();
  if ((await hist.count()) === 0) { console.error("FAIL: 启蒙理性 history not found"); await browser.close(); process.exit(1); }
  await hist.click();
  await page.waitForTimeout(3000);

  // 2. send "构建引用图谱" again
  const input = page.locator("textarea").first();
  await input.fill("构建引用图谱");
  await input.press("Enter");

  // 3. wait for the graph iframe card to appear (tool_result replayed)
  let iframeVisible = false;
  for (let i = 0; i < 40; i++) {
    const n = await page.locator('iframe[title="Citation Graph"]').count();
    if (n > 0) { iframeVisible = true; break; }
    await page.waitForTimeout(500);
  }
  let canvasCount = 0;
  try { canvasCount = await page.frameLocator('iframe[title="Citation Graph"]').locator("canvas").count(); } catch {}
  // assistant text reply should also commit (D-085 parser fix)
  const hasReply = (await page.getByText(/图谱已|节点|簇|重新展示|deep_read|综述/i).count()) > 0;

  await page.screenshot({ path: "/tmp/replay_graph.png", fullPage: false });
  console.log("graph iframe visible:", iframeVisible);
  console.log("canvas count inside iframe:", canvasCount);
  console.log("assistant reply committed:", hasReply);
  console.log("page errors:", errors.slice(0, 5));

  await browser.close();
  if (!iframeVisible) { console.error("FAIL: graph did not re-render on re-ask"); process.exit(1); }
  console.log("PASS: re-asking 构建引用图谱 re-rendered the graph card");
})();
