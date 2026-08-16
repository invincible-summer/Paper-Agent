/**
 * Playwright screenshot test for the chat citation graph (D-075).
 *
 * Loads /chat, opens a chat history item, and verifies the embedded pyvis
 * citation graph renders inside the iframe. Saves a PNG to /tmp/chat_graph.png.
 *
 * Usage:
 *   node scripts/screenshot-chat-graph.cjs [URL] [HISTORY-TEXT]
 * Defaults: URL=http://localhost:3000  HISTORY-TEXT=citation network
 *
 * Run after starting the dev servers. Requires the chat history to contain
 * at least one session with a built graph (graph_html persisted).
 */
const { chromium } = require("playwright");

const BASE = process.env.SCREENSHOT_BASE || process.argv[2] || "http://localhost:3000";
const URL = `${BASE}/chat`;
const HISTORY_TEXT = process.argv[3] || "citation network";
const OUT = "/tmp/chat_graph.png";

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-gpu"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push("PAGEERR: " + e.message));

  console.log("Navigating to", URL);
  await page.goto(URL, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1500);

  const target = page.getByText(new RegExp(HISTORY_TEXT, "i")).first();
  const found = (await target.count()) > 0;
  if (!found) {
    console.error(`History item matching /${HISTORY_TEXT}/ not found in sidebar`);
    await browser.close();
    process.exit(2);
  }
  await target.click();
  console.log("Clicked history item:", HISTORY_TEXT);
  await page.waitForTimeout(4000);

  const iframeSel = 'iframe[title="Citation Graph"]';
  let iframeVisible = false;
  try {
    await page.locator(iframeSel).waitFor({ state: "visible", timeout: 8000 });
    iframeVisible = true;
  } catch {
    console.log("iframe wait failed");
  }

  let canvasCount = 0;
  try {
    canvasCount = await page.frameLocator(iframeSel).locator("canvas").count();
  } catch {
    /* iframe not ready */
  }

  const clusterLegends = await page.locator("details:has(summary)").count();
  await page.screenshot({ path: OUT, fullPage: false });

  console.log("graph iframe visible:", iframeVisible);
  console.log("canvas count inside iframe:", canvasCount);
  console.log("cluster legends (details/summary):", clusterLegends);
  console.log("console errors:", errors.slice(0, 5));
  console.log("screenshot saved:", OUT);

  await browser.close();

  if (!iframeVisible || canvasCount === 0) {
    console.error("FAIL: graph did not render");
    process.exit(1);
  }
  console.log("PASS: graph rendered");
})();
