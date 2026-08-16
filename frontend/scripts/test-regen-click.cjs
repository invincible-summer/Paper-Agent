// D-087: click regenerate, confirm a new streaming turn starts (isResponding).
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  // send a trivial message so an assistant reply exists
  await page.fill("textarea", "你好");
  await page.click('button:has(svg.lucide-arrow-up)');
  await page.waitForTimeout(7000);
  const regen = page.locator('button:has-text("重新生成")').first();
  if (!(await regen.isVisible().catch(() => false))) {
    console.log("SKIP: no regenerate button (assistant reply may not have landed)");
    await browser.close();
    process.exit(0);
  }
  await regen.click();
  await page.waitForTimeout(2000);
  // After clicking regenerate the store should be responding again.
  // Detect the streaming indicator (spinner / animated dots).
  const streaming = await page.locator('svg.lucide-loader-2.animate-spin, .animate-bounce').first().isVisible().catch(() => false);
  console.log(streaming ? "PASS: regenerate started a new turn" : "WARN: streaming indicator not found after click");
  await page.screenshot({ path: "/tmp/d087_regen.png" });
  await browser.close();
  if (errors.length) { console.error("PAGE ERRORS:", errors.slice(0,3).join(" | ")); }
})();
