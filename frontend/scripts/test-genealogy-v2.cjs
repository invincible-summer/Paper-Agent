// E2E: genealogy graph v2 — fixed canvas, detail panel, 深问这篇 prefill.
// Usage: node scripts/test-genealogy-v2.cjs [BASE]
const { chromium } = require("playwright");
const BASE = process.env.SCREENSHOT_BASE || process.argv[2] || "http://localhost:3000";

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERR: " + e.message));

  await page.goto(`${BASE}/chat`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1200);

  // Open the GNN history session that has a persisted research map.
  const hist = page.getByText(/图神经网络在推荐系统中的应用/).first();
  if ((await hist.count()) === 0) { console.error("FAIL: history item not found"); process.exit(1); }
  await hist.click();
  await page.waitForTimeout(2500);

  // The graph may be below the fold inside the message list — scroll to it.
  const legend = page.getByText("思想源流（越老越粗）").first();
  let graphFound = false;
  for (let i = 0; i < 20; i++) {
    if ((await legend.count()) > 0) { graphFound = true; break; }
    await page.mouse.wheel(0, 800);
    await page.waitForTimeout(400);
  }
  if (!graphFound) { console.error("FAIL: genealogy graph not rendered"); await page.screenshot({ path: "/tmp/genealogy_v2_fail.png" }); process.exit(1); }
  await legend.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await page.screenshot({ path: "/tmp/genealogy_v2.png" });
  console.log("graph rendered: yes");

  // Fixed canvas check: svg height attribute-driven class h-[560px].
  const box = await legend.evaluate((el) => {
    const svg = el.closest("div.overflow-hidden")?.querySelector("svg");
    return svg ? svg.getBoundingClientRect() : null;
  });
  console.log("canvas box:", box && `${Math.round(box.width)}x${Math.round(box.height)}`);

  // Click a paper node (circle, not an aggregate rect) -> detail panel appears.
  const node = page.locator('svg g[style*="cursor: pointer"] circle').first();
  await node.click({ force: true });  // overlapping halo circles intercept; force is fine for e2e
  await page.waitForTimeout(600);
  const detail = page.getByText("它引用的").first();
  const detailOk = (await detail.count()) > 0;
  console.log("detail panel after node click:", detailOk);
  await page.screenshot({ path: "/tmp/genealogy_v2_detail.png" });

  // 深问这篇 prefills the composer.
  const askBtn = page.getByText("深问这篇").first();
  let prefill = "";
  if ((await askBtn.count()) > 0) {
    await askBtn.click();
    await page.waitForTimeout(400);
    prefill = await page.locator("textarea").first().inputValue();
  }
  console.log("composer prefill:", JSON.stringify(prefill.slice(0, 60)));
  await page.screenshot({ path: "/tmp/genealogy_v2_ask.png" });

  console.log("page errors:", errors.slice(0, 5));
  await browser.close();
  if (!detailOk || !prefill.includes("深问这篇论文")) { console.error("FAIL"); process.exit(1); }
  console.log("PASS: genealogy v2 graph + detail panel + deep-ask prefill");
})();
