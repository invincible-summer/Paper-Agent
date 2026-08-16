// D-090 residual-reset verification.
// Reproduces the user's complaint: after opening a structured/both session
// (topic "启蒙理性" / "女权主义"), (a) clicking "新建研究" must leave the
// structured workflow fully blank, and (b) opening a chat-only record then
// navigating back to / must ALSO be blank (no residual topic/papers from the
// previous structured session).
const { chromium } = require("playwright");

const BASE = process.env.BASE || "http://localhost:3000";
const BOTH_RECORD_TITLE = "启蒙理性";      // source=both (chat+structured, 10 papers)
const CHATONLY_RECORD_TITLE = "总结这篇论文"; // source=chat (no structured data)

const results = [];
function check(name, cond, detail = "") {
  results.push({ name, ok: !!cond, detail });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function topicInput(page) {
  return page.locator('input[type="text"]').first();
}
async function visibleText(page, sel) {
  const el = page.locator(sel);
  return (await el.count()) > 0 && (await el.first().isVisible());
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push(String(e)));

  // 1. Fresh entry to / should be blank.
  await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  let topic = await topicInput(page);
  check("1. fresh / topic blank", (await topic.inputValue()) === "",
    `topic="${await topic.inputValue()}"`);
  check("2. fresh / no papers section (Section 3)", !(await visibleText(page, 'h3:has-text("核心层")')),
    "core-layer heading should not be present on fresh entry");

  // 2. Open the both-source record (启蒙理性) from the sidebar.
  //    It restores structured state and stays on /.
  await page.locator(`text=${BOTH_RECORD_TITLE}`).first().click();
  await page.waitForTimeout(1200);
  topic = await topicInput(page);
  const topicVal = await topic.inputValue();
  check("3. after opening both-record, topic restored", topicVal.includes(BOTH_RECORD_TITLE),
    `topic="${topicVal}"`);
  check("4. after opening both-record, papers section visible", await visibleText(page, 'h3:has-text("核心层")'),
    "core-layer heading should appear after loading the both record");

  // 3. Click "新建研究" — must fully reset, no residual topic/papers.
  await page.locator('button:has-text("新建研究")').first().click();
  await page.waitForTimeout(500);
  topic = await topicInput(page);
  const tAfter = await topic.inputValue();
  check("5. after 新建研究, topic blank", tAfter === "", `topic="${tAfter}"`);
  check("6. after 新建研究, no papers section", !(await visibleText(page, 'h3:has-text("核心层")')),
    "core-layer heading must be gone after new project");
  check("7. after 新建研究, no residual subtopics section", !(await visibleText(page, "text=搜索策略")),
    "Section 2 (search strategy) must be gone after new project");

  // 4. Re-open the both record, then open a CHAT-only record (routes to /chat),
  //    then navigate back to / via the nav tab. Structured page must be blank.
  await page.locator(`text=${BOTH_RECORD_TITLE}`).first().click();
  await page.waitForTimeout(1200);
  topic = await topicInput(page);
  check("8. reopened both-record, topic restored again", (await topic.inputValue()).includes(BOTH_RECORD_TITLE));

  // Open chat-only record -> routes to /chat, and resetStructured() clears the
  // structured store.
  await page.locator(`text=${CHATONLY_RECORD_TITLE}`).first().click();
  await page.waitForTimeout(1500);
  check("9. navigated to /chat after chat-only record", page.url().includes("/chat"),
    `url=${page.url()}`);

  // Switch back to the structured workflow via the nav tab.
  await page.locator('a[href="/"]').first().click();
  await page.waitForTimeout(800);
  topic = await topicInput(page);
  const tFinal = await topic.inputValue();
  check("10. after chat-only + nav to /, topic blank (no residual)", tFinal === "",
    `topic="${tFinal}"`);
  check("11. after chat-only + nav to /, no residual papers", !(await visibleText(page, 'h3:has-text("核心层")')),
    "no core-layer heading should remain");
  check("12. no console errors", errors.length === 0, errors.slice(0, 3).join(" | "));

  await browser.close();
  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
