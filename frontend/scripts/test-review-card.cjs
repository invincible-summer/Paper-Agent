// D-086 e2e: a chat session whose literature_review lives at the top level
// (old history, review tool_call result had empty data) must still render
// the ReviewCard with the full review body after restoreChatState migration.
const { chromium } = require("playwright");
const BASE = process.env.SCREENSHOT_BASE || "http://localhost:3000";

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERR: " + e.message));

  await page.goto(`${BASE}/chat`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1200);
  const hist = page.getByText("女权主义").first();
  if ((await hist.count()) === 0) { console.error("FAIL: 女权主义 history not found"); await browser.close(); process.exit(1); }
  await hist.click();
  await page.waitForTimeout(3500);

  let exportBtn = false;
  for (let i = 0; i < 30; i++) {
    if ((await page.getByTitle("导出 Markdown").count()) > 0) { exportBtn = true; break; }
    await page.waitForTimeout(500);
  }
  const hasBody = (await page.getByText(/Wollstonecraft|Beauvoir|第二性|女权|gender/i).count()) > 0;
  await page.screenshot({ path: "/tmp/review_card.png", fullPage: false });

  console.log("ReviewCard export button visible:", exportBtn);
  console.log("review body text present:", hasBody);
  console.log("page errors:", errors.slice(0, 5));
  await browser.close();
  if (!exportBtn || !hasBody) { console.error("FAIL: review did not render in chat"); process.exit(1); }
  console.log("PASS: literature review renders inline in chat (ReviewCard)");
})();
