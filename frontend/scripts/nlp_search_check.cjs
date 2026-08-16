const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const events = [];
  page.on("console", (m) => { if (m.type() === "error") events.push(m.text()); });
  page.on("pageerror", (e) => events.push(String(e)));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle", timeout: 30000 });
  await page.locator('textarea').fill("NLP");
  await page.locator('textarea').press("Enter");

  let sawTool = false;
  try {
    await page.locator('text=/文献搜索|search tasks|搜索|papers|Found/i').first().waitFor({ state: "visible", timeout: 90000 });
    sawTool = true;
  } catch (e) {
    sawTool = false;
  }
  await page.screenshot({ path: "/tmp/nlp_search.png", fullPage: false });

  const body = (await page.locator("body").innerText()).slice(0, 400);
  if (sawTool) {
    console.log("PASS: search triggered for bare topic 'NLP'");
    console.log("body:", body.replace(/\n/g, " ").slice(0, 200));
  } else {
    console.log("FAIL: no search triggered within 90s (still giving a guide?)");
    console.log("body:", body.replace(/\n/g, " ").slice(0, 300));
    console.log("console errs:", events.slice(0, 3).join(" | "));
    process.exit(1);
  }
  await browser.close();
})();

