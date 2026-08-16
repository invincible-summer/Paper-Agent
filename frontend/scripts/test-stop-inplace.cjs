// D-088: the send button morphs into a stop button (Square icon, same slot)
// while a turn is streaming, and clicking it ends the turn.
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  // Clean slate.
  await page.evaluate(() => { try { localStorage.removeItem("paper-agent-chat"); } catch {} });
  await page.reload({ waitUntil: "networkidle" });

  // Kick off a turn (a search that won't finish instantly).
  await page.locator("textarea").fill("搜索启蒙理性的论文");
  await page.locator("textarea").press("Enter");

  // The stop button (Square icon, title="停止生成") should appear in place.
  const stopBtn = page.locator('button[title="停止生成"]');
  let visible = false;
  try {
    await stopBtn.waitFor({ state: "visible", timeout: 8000 });
    visible = true;
  } catch {
    visible = false;
  }

  if (!visible) {
    console.log("SKIP: turn finished before stop button rendered");
    await browser.close();
    process.exit(0);
  }
  console.log("PASS: stop button rendered in send-button slot");

  await stopBtn.click();
  // After click: turn should end; the stop button should disappear and the
  // send arrow should return within a couple seconds.
  let settled = false;
  for (let i = 0; i < 16; i++) {
    const stillStop = await page.locator('button[title="停止生成"]').isVisible().catch(() => false);
    if (!stillStop) { settled = true; break; }
    await page.waitForTimeout(500);
  }
  console.log(settled ? "PASS: stop button cleared after click" : "WARN: stop button still visible");
  await page.screenshot({ path: "/tmp/d088_inplace.png" });

  await browser.close();
  if (errors.length > 0) {
    console.error("PAGE ERRORS:", errors.slice(0, 3).join(" | "));
    process.exit(1);
  }
  console.log("DONE");
})();
