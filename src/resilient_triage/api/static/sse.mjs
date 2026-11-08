// @ts-check
// The page's network I/O: every fetch() the page makes goes through here, so the reducer and
// the components stay free of it (CODING_STANDARDS.md, Frontend).
//
// Server-sent events over fetch() (ADR 0008, #118). The trace streams answer a POST, which
// EventSource can't send, so this module reads the response body itself and parses the SSE
// framing the server writes: `event: <kind>` and one `data: <json>` line per event, blank-line
// separated, plus `: ping` comment blocks while the stream is idle.
//
// A *stream source* is a function of an AbortSignal that yields stream messages. The run
// controller in trace-reducer.mjs takes one, so tests and later panels can hand it any source.
//
// loadInto() fetches the page's JSON resources (graph-layout.json, GET /scenarios, GET
// /graph/nodes) into a Loadable signal.

/** @typedef {import("./trace-reducer.mjs").TraceEvent} TraceEvent */
/** @typedef {{ kind: "event", event: TraceEvent } | { kind: "ping" }} StreamMessage */
/** @typedef {(signal: AbortSignal) => AsyncIterable<StreamMessage>} StreamSource */
/**
 * The body of POST /triage/stream (schemas/api.py TriageRequest).
 * @typedef {{ query: string, service_filter?: string | null, force_refresh?: boolean }}
 *   TriageRequest
 */

/** The stream was refused before it started (404 unknown scenario, 422 bad request). */
export class StreamHttpError extends Error {
  /**
   * @param {number} status
   * @param {string} message
   */
  constructor(status, message) {
    super(message);
    this.name = "StreamHttpError";
    this.status = status;
  }
}

// --- Malformed streams ---
/** The stream arrived, but an event in it isn't valid JSON: not a connection failure. */
export class StreamDataError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = "StreamDataError";
  }
}

/**
 * An event's data parsed as JSON, or a StreamDataError when it isn't.
 * @param {string} data
 * @returns {any}
 */
function parseEventData(data) {
  try {
    return JSON.parse(data);
  } catch (error) {
    throw new StreamDataError(error instanceof Error ? error.message : String(error));
  }
}

/** An incremental SSE parser: push decoded text in any chunking, get whole messages back. */
function createSseParser() {
  let buffer = "";
  return {
    /**
     * @param {string} text
     * @returns {StreamMessage[]}
     */
    push(text) {
      buffer += text.replace(/\r\n?/g, "\n");
      /** @type {StreamMessage[]} */
      const messages = [];
      let end = buffer.indexOf("\n\n");
      while (end !== -1) {
        const message = parseBlock(buffer.slice(0, end));
        if (message) messages.push(message);
        buffer = buffer.slice(end + 2);
        end = buffer.indexOf("\n\n");
      }
      return messages;
    },
  };
}

/**
 * One blank-line separated block: an event, a ping, or nothing.
 * @param {string} block
 * @returns {StreamMessage | null}
 */
function parseBlock(block) {
  /** @type {string[]} */
  const data = [];
  let comment = false;
  for (const line of block.split("\n")) {
    if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    else if (line.startsWith(":")) comment = true;
  }
  if (data.length) return { kind: "event", event: parseEventData(data.join("\n")) };
  return comment ? { kind: "ping" } : null;
}

/**
 * Read a streaming response body as SSE messages.
 * @param {Response} response
 * @returns {AsyncGenerator<StreamMessage>}
 */
async function* readSse(response) {
  if (!response.body) return;
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = createSseParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      yield* parser.push(value);
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * POST to a stream route and yield its messages; throws StreamHttpError on a refusal.
 * @param {string} url
 * @param {unknown} body
 * @param {AbortSignal} signal
 * @returns {AsyncGenerator<StreamMessage>}
 */
async function* postStream(url, body, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new StreamHttpError(response.status, await refusalMessage(response));
  yield* readSse(response);
}

/**
 * The `detail` of a FastAPI error body, as one line.
 * @param {Response} response
 */
async function refusalMessage(response) {
  try {
    const { detail } = await response.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  } catch {
    // Not JSON: fall through to the status line.
  }
  return `The server answered ${response.status}.`;
}

/**
 * The stream of one guided scenario run (POST /scenarios/{id}/stream).
 * @param {string} scenarioId
 * @returns {StreamSource}
 */
export function scenarioSource(scenarioId) {
  const url = `/scenarios/${encodeURIComponent(scenarioId)}/stream`;
  return (signal) => postStream(url, undefined, signal);
}

/**
 * The stream of one free-play run (POST /triage/stream).
 * @param {TriageRequest} request
 * @returns {StreamSource}
 */
export function freePlaySource(request) {
  return (signal) => postStream("/triage/stream", request, signal);
}

/**
 * Fetch a JSON resource into a Loadable signal: "loading" while it's in flight, then the
 * value `pick` takes from the body, or "failed" on a non-2xx or a network error.
 * @template T
 * @param {{ value: import("./trace-reducer.mjs").Loadable<T> }} target a signal
 * @param {string} url
 * @param {(body: any) => T} [pick]
 */
export async function loadInto(target, url, pick = (body) => body) {
  target.value = "loading";
  try {
    const response = await fetch(url);
    target.value = response.ok ? pick(await response.json()) : "failed";
  } catch {
    target.value = "failed";
  }
}
