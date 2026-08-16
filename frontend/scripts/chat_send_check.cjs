// D-081 verify: chat page loads, SSE delivers an assistant reply (was "Failed to fetch").
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle", timeout: 30000 });
  await page.locator('textarea').fill("你好");
  await page.locator('textarea').press("Enter");

  // wait for an assistant reply to render (chat-prose is the assistant prose container)
  try {
    await page.locator(".chat-prose").first().waitFor({ state: "visible", timeout: 30000 });
    const text = (await page.locator(".chat-prose").first().innerText()).trim();
    console.log("PASS: assistant replied:", text.slice(0, 60).replace(/\n/g, " "));
  } catch (e) {
    await page.screenshot({ path: "/tmp/chat_send_fail.png" });
    console.log("FAIL: no assistant reply within 30s; console errs:", errs.join(" | ").slice(0, 200));
    process.exit(1);
  }

  await page.screenshot({ path: "/tmp/chat_send_ok.png", fullPage: false });
  await browser.close();
})();

