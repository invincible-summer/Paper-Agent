// End-to-end smoke: chat page → search → research map (incl. genealogy SVG).
// Usage: node scripts/e2e-smoke.cjs [baseURL]
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:3111";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(`${BASE}/chat`, { waitUntil: "networkidle" });
  console.log("page loaded, title:", await page.title());

  // Send a search request via the suggestion-free input.
  const input = page.locator("textarea").first();
  await input.fill("检索图神经网络推荐系统论文，然后生成研究地图");
  await input.press("Enter");
  console.log("message sent, waiting for research_map tool card...");

  // Wait until the research_map card appears (search ~60s + map ~40s).
  try {
    await page.waitForSelector("text=研究地图", { timeout: 240000 });
    console.log("research_map card visible");
    await page.waitForSelector("svg", { timeout: 30000 });
    console.log("genealogy svg visible");
  } catch (e) {
    console.log("TIMEOUT waiting for research_map:", String(e).slice(0, 120));
  }

  // Wait for the turn to finish (send button re-enabled).
  await page.waitForTimeout(8000);
  await page.screenshot({ path: "/tmp/e2e_chat_map.png", fullPage: false });
  console.log("screenshot: /tmp/e2e_chat_map.png");
  console.log("page errors:", errors.length ? errors : "none");
  await browser.close();
})();
