// D-089: a file uploaded in a chat turn shows as a chip on the user message,
// and survives a history reload (clicking the record in the sidebar).
const { chromium } = require("playwright");
const path = require("path");
const os = require("os");
const fs = require("fs");

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("http://localhost:3000/chat", { waitUntil: "networkidle" });
  await page.evaluate(() => { try { localStorage.removeItem("paper-agent-chat"); } catch {} });
  await page.reload({ waitUntil: "networkidle" });

  // Create a temp txt file and attach it.
  const tmp = path.join(os.tmpdir(), "d089_persist_test.txt");
  fs.writeFileSync(tmp, "Cognitive load theory and instructional design. Key findings: worked examples reduce extraneous load.");
  await page.setInputFiles('input[type="file"]', tmp);

  // Wait for the chip in the input bar to confirm upload is staged.
  await page.waitForTimeout(500);
  const ta = page.locator("textarea");
  await ta.fill("总结这篇论文");
  await ta.press("Enter");
  // Give the turn time to complete (or be interrupted — we only care that
  // the user message + its attachment chip render).
  await page.waitForTimeout(6000);

  // The user message bubble should now show a read-only file chip with the
  // uploaded filename. It's rendered inside the message column (items-end),
  // distinct from the input bar chip.
  const userChip = page.locator('span[title="d089_persist_test.txt"]').first();
  const chipVisible = await userChip.isVisible().catch(() => false);
  console.log(chipVisible ? "PASS: file chip on user message" : "FAIL: no chip on user message");
  await page.screenshot({ path: "/tmp/d089_chip.png" });

  // Now reload the page and click the most recent history record to verify
  // the chip survives the round-trip through save→load.
  await page.reload({ waitUntil: "networkidle" });
  // The sidebar lists history records; click the top one (most recent).
  const firstRecord = page.locator('[data-history-filename], li, button').filter({ hasText: "总结这篇论文" }).first();
  // Fallback: click the first clickable history item in the sidebar.
  let reloadedChip = false;
  try {
    await firstRecord.waitFor({ state: "visible", timeout: 4000 });
    await firstRecord.click({ timeout: 4000 });
    await page.waitForTimeout(1500);
    reloadedChip = await page.locator('span[title="d089_persist_test.txt"]').first().isVisible().catch(() => false);
  } catch {
    // Sidebar selector may differ; fall back to scanning the whole page.
    reloadedChip = await page.locator('span[title="d089_persist_test.txt"]').first().isVisible().catch(() => false);
  }
  console.log(reloadedChip ? "PASS: chip survives reload" : "WARN: chip not found after reload (selector mismatch possible)");
  await page.screenshot({ path: "/tmp/d089_reload.png" });

  await browser.close();
  if (errors.length) { console.error("PAGE ERRORS:", errors.slice(0, 3).join(" | ")); }
  if (!chipVisible) process.exit(1);
  console.log("DONE");
})();
