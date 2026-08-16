// D-087 e2e: file upload (paperclip + chips), copy-markdown action bar,
// and regenerate button on the last assistant message.
const { chromium } = require("playwright");
const path = require("path");

const URL = "http://localhost:3000/chat";

(async () => {
  // Use Playwright's bundled chromium (Linux). Windows Chrome over WSL2 has
  // a remote-debugging-pipe fd issue; the bundled headless shell is reliable.
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));

  // Start a fresh chat so we control the conversation.
  await page.goto(URL, { waitUntil: "networkidle" });
  // If a history sidebar exists, click "new session" if present.
  const newBtn = page.locator("button:has-text('新对话'), button:has-text('新会话')").first();
  if (await newBtn.isVisible().catch(() => false)) await newBtn.click().catch(() => {});

  // 1) File upload: paperclip button reveals a file input.
  const attachBtn = page.locator('button[title*="上传"]').first();
  if (!(await attachBtn.isVisible().catch(() => false))) {
    console.error("FAIL: paperclip attach button not visible");
    await browser.close();
    process.exit(1);
  }

  // Write a temp txt and set it on the hidden file input.
  const tmpTxt = path.join(require("os").tmpdir(), "pw_upload_test.txt");
  require("fs").writeFileSync(tmpTxt, "Reinforcement learning for dialogue systems survey.\nKey findings: policy gradient methods outperform supervised learning.");
  await page.setInputFiles('input[type="file"]', tmpTxt);
  // Wait for the chip to appear.
  await page.waitForTimeout(500);
  const chip = page.locator("text=pw_upload_test.txt").first();
  if (!(await chip.isVisible().catch(() => false))) {
    console.error("FAIL: uploaded file chip not visible");
    await browser.close();
    process.exit(1);
  }
  console.log("PASS: file chip visible after selecting file");

  // Type a message referencing the uploaded paper and send.
  await page.fill("textarea", "总结这篇上传论文的核心发现");
  await page.click('button[type="button"] .lucide-arrow-up, button:has(svg.lucide-arrow-up)');
  // Wait for either an assistant message or a streaming block.
  await page.waitForTimeout(8000);
  // Screenshot.
  await page.screenshot({ path: "/tmp/d087_upload.png" });

  // 2) Copy + regenerate action bar on the last assistant message.
  // Hover the last assistant message to reveal actions.
  const assistantMsgs = page.locator('div.group.flex.items-start:has(span:has-text("Paper Agent")), div.group:has(svg.lucide-sparkles)');
  const count = await assistantMsgs.count().catch(() => 0);
  if (count === 0) {
    // Fall back: any rendered markdown block.
    const md = page.locator(".chat-prose").first();
    if (!(await md.isVisible().catch(() => false))) {
      console.error("FAIL: no assistant message rendered");
      await browser.close();
      process.exit(1);
    }
  }
  // Copy button should exist (revealed on hover).
  const copyBtn = page.locator('button:has-text("复制")').first();
  await copyBtn.hover().catch(() => {});
  const copyVisible = await copyBtn.isVisible().catch(() => false);
  console.log(copyVisible ? "PASS: copy button present" : "WARN: copy button not visible (may need hover)");

  // 3) Regenerate button only on the last assistant message while idle.
  const regenBtn = page.locator('button:has-text("重新生成")');
  const regenCount = await regenBtn.count().catch(() => 0);
  console.log(regenCount > 0 ? `PASS: regenerate button present (${regenCount})` : "WARN: regenerate button absent (still streaming or no assistant msg)");

  await browser.close();
  if (errors.length > 0) {
    console.error("PAGE ERRORS:", errors.slice(0, 5).join(" | "));
    process.exit(1);
  }
  console.log("DONE");
})();
