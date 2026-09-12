const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("http://localhost:3000", { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1000);
  await page.getByRole("button", { name: /Settings|设置/ }).first().click();
  await page.waitForTimeout(500);
  const hasLanguage = await page.getByText(/Language|语言/).first().isVisible().catch(() => false);
  // Use an exact label: the chat welcome copy legitimately contains the word
  // “主题” in phrases such as “主题簇”, which is not a theme control.
  const hasTheme = await page.getByText(/^(Theme|主题)$/).first().isVisible().catch(() => false);
  const hasRetiredNetworkControl = await page.getByText(/PDF probe|remote full.?text|网络全文探测|自动全文升级/i).first().isVisible().catch(() => false);
  await page.screenshot({ path: "/tmp/settings_popover.png" });
  console.log("language setting visible:", hasLanguage);
  console.log("theme setting visible (must be false, dark mode removed):", hasTheme);
  console.log("retired network-fulltext control visible:", hasRetiredNetworkControl);
  console.log("page errors:", errors.slice(0, 3));
  await browser.close();
  if (!hasLanguage || hasTheme || hasRetiredNetworkControl) {
    console.error("FAIL: settings popover does not match the current capability boundary");
    process.exit(1);
  }
  console.log("PASS: language is visible; theme and retired network-fulltext controls are absent");
})();
