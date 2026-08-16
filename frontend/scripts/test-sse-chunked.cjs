// D-085 regression test for the chatStream SSE parser.
//
// Proves: when an SSE event's data: line is large enough to span multiple
// reader.read() chunks (the graph tool_result / done events embed the full
// pyvis graph_html, ~692KB in the real 启蒙理性 session), the event type must
// persist across chunks. The buggy parser declared currentEvent INSIDE the
// while loop, resetting it to "message" each chunk, so the tool_result and the
// final done event were silently dropped and the assistant summary vanished
// until a manual refresh.
//
// Replicates the chatStream parser logic from lib/chat-api.ts (the .ts module
// can't be imported by bare node without a TS loader; the build's type check
// covers the real file, this covers the parsing CONTRACT).
//
// Run: node scripts/test-sse-chunked.cjs

const BIG = "x".repeat(700000); // ~692KB, like the real graph_html payload

function sseFrame(eventType, payload) {
  return `event: ${eventType}\ndata: ${JSON.stringify(payload)}\n\n`;
}

const frames =
  sseFrame("tool_result", { result: { graph_html: BIG, n_nodes: 10, n_edges: 12 } }) +
  sseFrame("answer", { content: "summary", is_delta: true }) +
  sseFrame("done", { answer: "summary", tool_calls: [{ name: "graph", result: { graph_html: BIG } }] });

const fullBytes = new TextEncoder().encode(frames);
const CHUNK = 4096;
const chunks = [];
for (let i = 0; i < fullBytes.length; i += CHUNK) {
  chunks.push(fullBytes.slice(i, i + CHUNK));
}

function makeReader(cs) {
  let i = 0;
  return { async read() { if (i >= cs.length) return { done: true, value: undefined }; return { done: false, value: cs[i++] }; } };
}

// FIXED parser: currentEvent declared OUTSIDE the while loop (persists).
async function* parseFixed(reader) {
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "message";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("event:")) {
        currentEvent = trimmed.slice(6).trim();
      } else if (trimmed.startsWith("data:")) {
        try { const p = JSON.parse(trimmed.slice(5).trim()); p.type = currentEvent; yield p; } catch {}
      }
    }
  }
}

// BUGGY parser: currentEvent INSIDE the while loop (resets each chunk).
async function* parseBuggy(reader) {
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    let currentEvent = "message";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("event:")) {
        currentEvent = trimmed.slice(6).trim();
      } else if (trimmed.startsWith("data:")) {
        try { const p = JSON.parse(trimmed.slice(5).trim()); p.type = currentEvent; yield p; } catch {}
      }
    }
  }
}

async function collect(gen) { const out = []; for await (const e of gen) out.push(e); return out; }

(async () => {
  let failures = 0;

  const fixed = await collect(parseFixed(makeReader(chunks)));
  const fixedTypes = fixed.map(e => e.type);
  const fixedToolResult = fixed.find(e => e.type === "tool_result");
  console.log("[fixed] events:", fixedTypes.join(","));
  console.log("[fixed] tool_result present:", !!fixedToolResult,
    "| graph_html kept:", fixedToolResult ? fixedToolResult.result.graph_html.length : 0);
  if (!fixedTypes.includes("tool_result") || !fixedTypes.includes("done")) {
    console.error("FAIL [fixed]: expected tool_result + done events"); failures++;
  }

  const buggy = await collect(parseBuggy(makeReader(chunks)));
  const buggyTypes = buggy.map(e => e.type);
  console.log("[buggy] events:", buggyTypes.join(","));
  if (buggyTypes.includes("tool_result") || buggyTypes.includes("done")) {
    console.error("FAIL [buggy]: expected buggy parser to DROP tool_result + done (regression guard)"); failures++;
  } else {
    console.log("[buggy] confirmed: large events dropped (type reset to 'message') — pre-fix bug reproduced");
  }

  if (failures > 0) process.exit(1);
  console.log("PASS: fixed parser keeps large-event types; buggy parser reproduces the drop");
})();
