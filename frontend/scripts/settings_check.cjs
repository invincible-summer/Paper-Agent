const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("http://localhost:3000", { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1000);
  // Open settings popover (the Settings button in the header)
  await page.getByRole("button", { name: /Settings|设置/ }).first().click();
  await page.waitForTimeout(500);
  // Find the Deep Read label text
  const hasDeepReadLabel = await page.getByText(/Deep Read mode|深度阅读模式/).first().isVisible().catch(() => false);
  const hasAbstractBtn = await page.getByText(/^Abstract$|^仅摘要$/).first().isVisible().catch(() => false);
  const hasFullBtn = await page.getByText(/Full text|全文 PDF/).first().isVisible().catch(() => false);
  await page.screenshot({ path: "/tmp/settings_deepread.png" });
  console.log("deepRead label visible:", hasDeepReadLabel);
  console.log("abstract button visible:", hasAbstractBtn);
  console.log("full button visible:", hasFullBtn);
  console.log("page errors:", errors.slice(0, 3));
  await browser.close();
  if (!hasDeepReadLabel) { console.error("FAIL: settings toggle missing"); process.exit(1); }
  console.log("PASS: settings toggle present");
})();
