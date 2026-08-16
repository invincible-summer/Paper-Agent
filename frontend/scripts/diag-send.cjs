const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  const ta = page.locator("textarea").first();
  const taVisible = await ta.isVisible();
  console.log("textarea visible:", taVisible);
  await ta.fill("搜索启蒙理性的论文");
  const val = await ta.inputValue();
  console.log("textarea value:", JSON.stringify(val));
  const btnCount = await page.locator('button:has(svg.lucide-arrow-up)').count();
  console.log("arrow-up button count:", btnCount);
  if (btnCount > 0) {
    const b = page.locator('button:has(svg.lucide-arrow-up)').first();
    console.log("button disabled attr:", await b.getAttribute("disabled"));
    console.log("button isEnabled:", await b.isEnabled());
  }
  await page.screenshot({ path: "/tmp/diag.png" });
  await browser.close();
})();
