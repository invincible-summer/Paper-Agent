// D-091 right sidebar + collapsible sidebars verification.
// Opens the chat-only record "总结这篇上传论文的核心发现" (session-level
// attachments, no per-message attachments — the harder old-record case),
// then drives the right sidebar: list tab -> click item -> viewer -> collapse,
// plus left sidebar collapse/expand.
const { chromium } = require("playwright");

const BASE = process.env.BASE || "http://localhost:3000";
const RECORD_TITLE = "总结这篇上传论文的核心发现";
const FILENAME = "WiFi与网络通信.pdf";

const results = [];
function check(name, cond, detail = "") {
  results.push({ name, ok: !!cond, detail });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));

  // Right sidebar collapsed by default so we exercise the expand-from-rail path.
  await page.addInitScript(() => {
    localStorage.setItem("paper-agent-left-sidebar-open", "1");
    localStorage.setItem("paper-agent-right-sidebar-open", "0");
  });
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(600);
  check("1. fresh / — right rail expand btn present (chat-only)",
    (await page.locator('button[title="展开右边栏"]').count()) === 0,
    "right rail should NOT render on / (chat-only)");

  // Open the chat-only record -> routes to /chat. Right sidebar still collapsed.
  await page.locator(`text=${RECORD_TITLE}`).first().click();
  await page.waitForTimeout(1500);
  check("2. navigated to /chat", page.url().includes("/chat"), `url=${page.url()}`);
  check("3. right sidebar collapsed rail present on /chat",
    (await page.locator('button[title="展开右边栏"]').count()) > 0);

  // Expand the right sidebar from the rail.
  await page.locator('button[title="展开右边栏"]').first().click();
  await page.waitForTimeout(400);
  check("4. right sidebar expanded", (await page.locator('button[title="收起右边栏"]').count()) > 0);

  // List tab should show the two uploaded files (session-level).
  const listTab = page.locator('button:has-text("上传列表")').first();
  check("5. list tab button present", await listTab.count() > 0);
  await listTab.click();
  await page.waitForTimeout(300);
  check("6. list shows 本会话上传文件 heading",
    (await page.locator('text=本会话上传文件').first().isVisible()));
  const fileRow = page.locator(`aside button:has-text("${FILENAME}")`).first();
  check("7. list shows the uploaded file row", await fileRow.isVisible());

  // Click the file row -> viewer tab opens and fetches content.
  await fileRow.click();
  await page.waitForTimeout(1500);
  check("8. viewer switched in (filename header visible)",
    (await page.locator(`text=${FILENAME}`).first().isVisible()));
  // The right sidebar panel TabButton for viewer should be active.
  check("9. viewer tab active",
    (await page.locator('button:has-text("文件预览")').first().isVisible()));

  // Collapse the right sidebar.
  await page.locator('button[title="收起右边栏"]').first().click();
  await page.waitForTimeout(400);
  check("10. right sidebar collapsed back to rail",
    (await page.locator('button[title="展开右边栏"]').count()) > 0);

  // Left sidebar collapse + expand.
  const closeLeft = page.locator('button[title="收起左边栏"]').first();
  check("11. collapse-left button present", await closeLeft.count() > 0);
  await closeLeft.click();
  await page.waitForTimeout(400);
  check("12. left sidebar collapsed to rail",
    (await page.locator('button[title="展开左边栏"]').count()) > 0);
  // New-session button still reachable in the collapsed rail.
  check("13. left rail has new-session button",
    (await page.locator('button[title="新建研究"]').count()) > 0);
  await page.locator('button[title="展开左边栏"]').first().click();
  await page.waitForTimeout(400);
  check("14. left sidebar re-expanded",
    (await page.locator('button[title="收起左边栏"]').count()) > 0);

  check("15. no page errors", errors.length === 0, errors.slice(0, 3).join(" | "));

  await browser.close();
  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
