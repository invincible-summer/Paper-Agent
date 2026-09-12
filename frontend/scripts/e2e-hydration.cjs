const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const runtimeErrors = [];
  const backendErrors = [];
  page.on("pageerror", (e) => runtimeErrors.push(String(e).slice(0, 200)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text().slice(0, 200);
    const url = m.location()?.url || "";
    // The frontend proxies these API calls to the optional local backend. A
    // stopped backend should be visible in the report without masking React
    // hydration or runtime errors in the page itself.
    if (/\/api\/v1\//.test(url) || /localhost:8000|127\.0\.0\.1:8000|ECONNREFUSED/i.test(text + url)) {
      backendErrors.push(text);
    } else {
      runtimeErrors.push(text);
    }
  });
  // 1) default (sidebar states as server default)
  await page.goto("http://127.0.0.1:3000/chat", { waitUntil: "networkidle" });
  // 2) simulate persisted prefs (collapsed sidebars + a stale theme key), reload
  await page.evaluate(() => {
    localStorage.setItem("paper-agent-left-sidebar-open", "0");
    localStorage.setItem("paper-agent-right-sidebar-open", "0");
    localStorage.setItem("paper-agent-theme", "dark"); // stale key from the removed dark mode
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const dark = await page.evaluate(() => document.documentElement.classList.contains("dark"));
  const hydrationErrors = runtimeErrors.filter(e => /hydrat|Hydration/i.test(e));
  console.log("dark class absent (dark mode removed):", !dark);
  console.log("hydration errors:", hydrationErrors.length ? hydrationErrors : "none");
  console.log("backend resource errors (ignored):", backendErrors.length ? backendErrors : "none");
  console.log("all page errors:", runtimeErrors.length ? runtimeErrors : "none");
  if (hydrationErrors.length || runtimeErrors.length) process.exitCode = 1;
  await browser.close();
})();
