const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
  // 1) default (sidebar states as server default)
  await page.goto("http://127.0.0.1:3001/chat", { waitUntil: "networkidle" });
  // 2) simulate persisted prefs (collapsed left sidebar + dark theme), reload
  await page.evaluate(() => {
    localStorage.setItem("paper-agent-left-sidebar-open", "0");
    localStorage.setItem("paper-agent-right-sidebar-open", "0");
    localStorage.setItem("paper-agent-theme", "dark");
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const dark = await page.evaluate(() => document.documentElement.classList.contains("dark"));
  const hydrationErrors = errors.filter(e => /hydrat|Hydration/i.test(e));
  console.log("dark theme applied:", dark);
  console.log("hydration errors:", hydrationErrors.length ? hydrationErrors : "none");
  console.log("all page errors:", errors.length ? errors : "none");
  await browser.close();
})();
