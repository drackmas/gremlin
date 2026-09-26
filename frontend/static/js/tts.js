/* Piper TTS — sentence-level stream buffer + Web Audio playback.
 *
 * Feed assistant `text` deltas as they arrive from the chat SSE stream via
 * push(). As soon as a complete sentence is detected (".!?" followed by
 * whitespace), it is queued and synthesized; playback starts while the rest of
 * the reply is still streaming. flush() is called when the stream ends and
 * speaks any remaining trailing text. stop() aborts everything in flight (new
 * user message, stream error, or the setting toggled off).
 */
"use strict";

const tts = (() => {
  const MAX_SENTENCE_CHARS = 2000; // must match the backend MAX_TEXT_CHARS
  let ctx = null;          // shared AudioContext (created lazily on user gesture)
  let on = false;          // TTS active for the current message
  let gen = 0;             // bumped on start()/stop() so stale work is ignored
  let buffer = "";         // in-progress text not yet split into sentences
  let queue = [];          // sentences waiting to be synthesized
  let inflight = null;     // Promise for the sentence currently being fetched
  let inflightAbort = null; // AbortController for the in-flight synthesis fetch
  let sources = new Set(); // AudioBufferSourceNodes currently scheduled
  let nextStartT = 0;      // audio-clock time the next buffer should begin

  function audioCtx() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      ctx = new AC();
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }

  // Pull complete sentences off the front of `buffer`, returning them and
  // leaving the remainder. A sentence ends at ".!?" followed by whitespace
  // (or a newline). Trailing text stays buffered until the next delta/flush.
  function takeSentences() {
    const out = [];
    for (;;) {
      let idx = -1;
      for (let i = 0; i < buffer.length; i++) {
        const c = buffer[i];
        if (c === "." || c === "!" || c === "?") {
          const n = buffer[i + 1];
          if (n === " " || n === "\n" || n === "\r" || n === "\t") {
            idx = i;
            break;
          }
        }
      }
      if (idx === -1) break;
      const sent = buffer.slice(0, idx + 1).trim();
      buffer = buffer.slice(idx + 1);
      if (sent) out.push(sent);
    }
    return out;
  }

  // Enqueue a sentence, splitting into <= MAX_SENTENCE_CHARS pieces so the
  // backend (which caps text length) never rejects it.
  function enqueue(text) {
    let t = text.trim();
    while (t.length > MAX_SENTENCE_CHARS) {
      let cut = t.lastIndexOf(" ", MAX_SENTENCE_CHARS);
      if (cut < MAX_SENTENCE_CHARS / 2) cut = MAX_SENTENCE_CHARS;
      queue.push(t.slice(0, cut).trim());
      t = t.slice(cut).trim();
    }
    if (t) queue.push(t);
  }

  function scheduleBuffer(f32, rate) {
    const c = audioCtx();
    if (!c || c.state === "closed" || !f32.length) return;
    const ab = c.createBuffer(1, f32.length, rate);
    ab.getChannelData(0).set(f32);
    const src = c.createBufferSource();
    src.buffer = ab;
    src.connect(c.destination);
    let t = c.currentTime + 0.03; // small gap between sentences
    if (nextStartT > t) t = nextStartT;
    nextStartT = t + ab.duration;
    sources.add(src);
    src.onended = () => {
      sources.delete(src);
      if (sources.size === 0 && queue.length === 0 && !inflight) nextStartT = 0;
    };
    src.start(t);
  }

  async function fetchPcm(text, signal) {
    const res = await fetch("/api/tts/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal,
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try {
        const j = await res.json();
        if (j && j.error) detail = j.error;
      } catch (_) { /* keep HTTP status */ }
      throw new Error(detail);
    }
    const rate =
      parseInt(res.headers.get("X-Audio-Sample-Rate") || "22050", 10) || 22050;
    const buf = await res.arrayBuffer();
    const s16 = new Int16Array(buf);
    const f32 = new Float32Array(s16.length);
    for (let i = 0; i < s16.length; i++) f32[i] = s16[i] / 32768;
    return { f32, rate };
  }

  // Synthesize at most one queued sentence at a time (keeps playback in order).
  // Each fetch carries its own AbortController so stop()/start() can kill a
  // request that is still in flight (a zombie request would keep the backend
  // synthesizing for a sentence nobody will hear).
  function pump(g) {
    if (g !== gen || inflight || !queue.length) return;
    const text = queue.shift();
    const abort = new AbortController();
    inflightAbort = abort;
    const run = fetchPcm(text, abort.signal)
      .then(({ f32, rate }) => {
        if (g === gen) scheduleBuffer(f32, rate); // ignore if stopped/started
      })
      .catch((e) => {
        if (!abort.signal.aborted) console.warn("TTS: " + e.message);
      })
      .finally(() => {
        // Only clear state we still own: a newer run may have started a new
        // fetch before this (aborted) one settled.
        if (inflight === run) inflight = null;
        if (inflightAbort === abort) inflightAbort = null;
        if (g === gen) pump(g);
      });
    inflight = run;
  }

  function stopSources() {
    for (const s of sources) {
      try {
        s.onended = null;
        s.stop();
      } catch (_) { /* already stopped */ }
    }
    sources.clear();
  }

  return {
    // Begin TTS for a new assistant message (aborts any previous run,
    // including a synthesis request still in flight).
    start() {
      gen += 1;
      on = true;
      buffer = "";
      queue = [];
      nextStartT = 0;
      if (inflightAbort) {
        inflightAbort.abort();
        inflightAbort = null;
      }
      stopSources();
    },

    // Feed an assistant text delta; speaks each completed sentence.
    push(delta) {
      if (!on || !delta) return;
      buffer += delta;
      for (const sent of takeSentences()) enqueue(sent);
      pump(gen);
    },

    // Stream ended: speak the remaining trailing text, if any.
    flush() {
      if (!on) return;
      const rest = buffer.trim();
      buffer = "";
      if (rest) enqueue(rest);
      pump(gen);
    },

    // Abort all TTS (new user message / stream error / toggle off):
    // stops scheduled audio, clears the sentence queue, and cancels a
    // synthesis request that is still in flight.
    stop() {
      gen += 1;
      on = false;
      buffer = "";
      queue = [];
      nextStartT = 0;
      if (inflightAbort) {
        inflightAbort.abort();
        inflightAbort = null;
      }
      stopSources();
    },

    get enabled() {
      return on;
    },

    // True while audio is scheduled, queued, or being synthesized. STT uses
    // this to wait for speaker output to stop after a barge-in interrupt.
    get playing() {
      return on && (sources.size > 0 || queue.length > 0 || inflight !== null);
    },
  };
})();
