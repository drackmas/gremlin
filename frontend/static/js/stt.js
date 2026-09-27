/* stt.js — client-side speech capture for speech-to-text.
 *
 * Modeled on AIVTuber's STT: the mic is sampled continuously and each audio
 * block is RMS-checked against a dB-scale threshold (0–100 % on −60..0 dB).
 * An utterance starts when a block crosses the threshold and ends after the
 * configured silence duration of inactivity.  Leading silence is trimmed and
 * a short tail is kept after the last active block so trailing consonants
 * aren't clipped.
 *
 * Two modes:
 *   hold       — press-and-hold the mic; the window is transcribed and placed
 *                in the input for edit/Send (never auto-sent).
 *   continuous — tap the mic to listen; each utterance is transcribed and
 *                auto-sent after a pause, then listening resumes.
 *
 * Audio is captured in the browser, resampled to 16 kHz mono s16le, wrapped
 * in a minimal WAV header and POSTed to /api/stt/transcribe.  The mic
 * permission is requested only on the first interaction with the mic button.
 */
const stt = (() => {
  const TARGET_RATE = 16000;
  const BLOCK = 4096;               // ScriptProcessorNode buffer size
  const DB_MIN = -60.0;             // dB RMS mapped to 0 % volume
  const TAIL = 0.2;                 // seconds kept after the last active block
  const MIN_SECONDS = 0.2;          // shorter than this is dropped
  const MAX_UTTERANCE = 300.0;      // force-cut very long utterances

  // Convert a 0–100 % volume (dB scale) to an RMS threshold.
  function pctToRms(pct) {
    const db = DB_MIN + (Math.max(0, Math.min(100, pct)) / 100.0) * -DB_MIN;
    return Math.pow(10.0, db / 20.0);
  }

  // Convert an RMS value to a 0–100 % volume (dB scale).
  function rmsToPct(rms) {
    const db = 20.0 * Math.log10(Math.max(rms, 1e-9));
    return Math.max(0, Math.min(100, (db - DB_MIN) / -DB_MIN * 100.0));
  }

  let cfg = { enabled: false, mode: "hold", model: "base", threshold: 30, silence: 1.0, debug: false };

  let ctx = null;      // AudioContext
  let holdQueued = false;   // press arrived while the mic graph was being set up
  let holdReleased = false; // release arrived while the mic graph was being set up
  let stream = null;   // MediaStream (mic)
  let analyser = null; // AnalyserNode (RMS)
  let proc = null;     // ScriptProcessorNode (sample accumulation)
  let listening = false; // mic running (hold: while pressed; continuous: on)

  // Block-based utterance accumulation (matches AIVTuber).
  let blocks = [];      // array of Float32Array (raw mic chunks)
  let active = [];      // array of booleans (RMS >= threshold)
  let inUtterance = false;
  let dropUntil = 0;   // ignore samples until this time (TTS tail decay)
  let stateName = "idle"; // "idle" | "listening" | "recording" | "transcribing"

  const hooks = { speechStart: null, utterance: null, state: null, error: null, level: null };

  function configure(c) { Object.assign(cfg, c); }

  function setState(s) { stateName = s; if (hooks.state) hooks.state(s); }

  function clearBlocks() {
    blocks.length = 0;
    active.length = 0;
    inUtterance = false;
  }

  function fail(msg) {
    listening = false;
    clearBlocks();
    if (hooks.error) hooks.error(msg);
    setState("idle");
  }

  /* --- audio graph (created lazily on first mic interaction) ------------ */

  async function ensureGraph() {
    if (stream) return;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
    });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    proc = ctx.createScriptProcessor(BLOCK, 1, 1);
    // ScriptProcessor only fires while connected to the destination; route
    // through a zero-gain node so the mic is not fed back to the speakers.
    const silent = ctx.createGain();
    silent.gain.value = 0;
    src.connect(analyser);
    analyser.connect(proc);
    proc.connect(silent);
    silent.connect(ctx.destination);
    proc.onaudioprocess = onAudio;
  }

  async function startMic() {
    await ensureGraph();
    if (ctx.state === "suspended") await ctx.resume();
  }

  function onAudio(ev) {
    if (!listening) return;
    const data = ev.inputBuffer.getChannelData(0);
    const rms = Math.sqrt(data.reduce((s, v) => s + v * v, 0) / data.length);

    // Report the level for the volume meter.
    if (hooks.level) hooks.level(rms);

    if (cfg.debug)
      console.debug("[stt] rms=%.4f thr=%.4f pct=%.0f%% inUtter=%s blocks=%d",
        rms, pctToRms(cfg.threshold), rmsToPct(rms), inUtterance, blocks.length);

    // While dropping (TTS tail decay), don't accumulate.
    if (performance.now() < dropUntil) return;

    // Accumulate the block and its active flag.
    blocks.push(new Float32Array(data));
    active.push(rms >= pctToRms(cfg.threshold));

    // Cap buffer length (MAX_UTTERANCE seconds).
    const maxBlocks = Math.floor(MAX_UTTERANCE * ctx.sampleRate / BLOCK) + 50;
    if (blocks.length > maxBlocks) { blocks.shift(); active.shift(); }

    if (cfg.mode === "continuous") poll();
  }

  // Block-based utterance detection (matches AIVTuber's _poll).
  // Trims leading silence, then ends the utterance once the trailing
  // inactivity reaches the configured silence duration (or MAX_UTTERANCE).
  function poll() {
    // Trim leading silence.
    while (active.length && !active[0]) { blocks.shift(); active.shift(); }
    if (!blocks.length) { inUtterance = false; return; }

    if (!inUtterance) {
      if (!active.some(Boolean)) { inUtterance = false; return; }
      inUtterance = true;
      setState("recording");
      if (hooks.speechStart) hooks.speechStart();
    }

    // Trailing silence check.
    let trailing = 0.0;
    for (let i = active.length - 1; i >= 0; i--) {
      if (active[i]) break;
      trailing += BLOCK / ctx.sampleRate;
    }
    const duration = blocks.length * BLOCK / ctx.sampleRate;
    if (!(trailing >= cfg.silence || duration >= MAX_UTTERANCE)) return;

    // Keep up to the last active block + tail.
    let lastActive = 0;
    for (let i = 0; i < active.length; i++) if (active[i]) lastActive = i;
    const tailBlocks = Math.ceil(TAIL * ctx.sampleRate / BLOCK);
    const keep = Math.min(blocks.length, lastActive + 1 + tailBlocks);
    const audio = concatFloat32(blocks.slice(0, keep));
    clearBlocks();
    if (audio.length >= TARGET_RATE * MIN_SECONDS) {
      transcribe(audio, true);
    } else {
      setState("listening");
    }
  }

  // Finalize a hold-mode capture: trim leading silence, keep a tail.
  function finalizeHold() {
    while (active.length && !active[0]) { blocks.shift(); active.shift(); }
    if (!blocks.length) return new Float32Array(0);
    let lastActive = 0;
    for (let i = 0; i < active.length; i++) if (active[i]) lastActive = i;
    const tailBlocks = Math.ceil(TAIL * ctx.sampleRate / BLOCK);
    const keep = Math.min(blocks.length, lastActive + 1 + tailBlocks);
    return concatFloat32(blocks.slice(0, keep));
  }

  function concatFloat32(arrays) {
    const total = arrays.reduce((s, a) => s + a.length, 0);
    const out = new Float32Array(total);
    let off = 0;
    for (const a of arrays) { out.set(a, off); off += a.length; }
    return out;
  }

  function peakOf(samples) {
    let peak = 0;
    for (let i = 0; i < samples.length; i++) {
      const a = Math.abs(samples[i]);
      if (a > peak) peak = a;
    }
    return peak;
  }

  /* --- WAV encoding (resamples to 16 kHz mono s16le) -------------------- */

  function resample(mono, srcRate) {
    if (srcRate === TARGET_RATE) return mono;
    const n = Math.max(1, Math.round((mono.length * TARGET_RATE) / srcRate));
    const out = new Float32Array(n);
    const step = mono.length / n;
    for (let i = 0; i < n; i++) {
      const pos = i * step;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, mono.length - 1);
      const f = pos - i0;
      out[i] = mono[i0] * (1 - f) + mono[i1] * f;
    }
    return out;
  }

  function encodeWav(samples, srcRate) {
    const mono = resample(samples, srcRate);
    const bytes = new ArrayBuffer(44 + mono.length * 2);
    const dv = new DataView(bytes);
    const ws = (o, s) => {
      for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i));
    };
    ws(0, "RIFF");
    dv.setUint32(4, 36 + mono.length * 2, true);
    ws(8, "WAVE");
    ws(12, "fmt ");
    dv.setUint32(16, 16, true);
    dv.setUint16(20, 1, true); // PCM
    dv.setUint16(22, 1, true); // mono
    dv.setUint32(24, TARGET_RATE, true);
    dv.setUint32(28, TARGET_RATE * 2, true);
    dv.setUint16(32, 2, true);
    dv.setUint16(34, 16, true);
    ws(36, "data");
    dv.setUint32(40, mono.length * 2, true);
    let o = 44;
    for (let i = 0; i < mono.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, mono[i]));
      dv.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Uint8Array(bytes);
  }

  // Chunked base64 so long utterances don't blow the call stack.
  function uint8ToBase64(bytes) {
    let bin = "";
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(bin);
  }

  /* --- transcription ----------------------------------------------------- */

  async function transcribe(samples, autoSend) {
    if (!ctx || !samples.length) return;
    setState("transcribing");
    try {
      const wav = encodeWav(samples, ctx.sampleRate);
      const res = await fetch("/api/stt/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio: uint8ToBase64(wav), model: cfg.model }),
      });
      let detail = `HTTP ${res.status}`;
      let text = "";
      try {
        const j = await res.json();
        if (j && j.error) detail = j.error;
        if (j && j.text) text = j.text;
      } catch (_) {}
      if (!res.ok) throw new Error(detail);
      if (hooks.utterance) hooks.utterance(text, autoSend);
    } catch (e) {
      if (hooks.error) hooks.error(e.message);
    } finally {
      setState(listening ? (cfg.mode === "continuous" ? "listening" : "idle") : "idle");
    }
  }

  /* --- public handlers (wired to the mic button in app.js) --------------- */

  async function holdStart() {
    if (!cfg.enabled || listening || holdQueued) return;
    holdQueued = true;
    holdReleased = false;
    const wasSpeaking = typeof tts !== "undefined" && tts.playing;
    if (hooks.speechStart) hooks.speechStart(); // barge in: stop TTS + generation
    try {
      await startMic();
    } catch (e) {
      holdQueued = false;
      fail(e.message || "microphone unavailable");
      return;
    }
    holdQueued = false;
    if (holdReleased) {
      // Released before the mic was ready: nothing was captured.
      setState("idle");
      return;
    }
    listening = true;
    clearBlocks();
    dropUntil = wasSpeaking ? performance.now() + 250 : 0;
    setState("recording");
  }

  function holdEnd() {
    if (holdQueued) {
      holdReleased = true; // holdStart sees this once the mic is ready
      return;
    }
    if (!listening) return;
    listening = false;
    const samples = finalizeHold();
    clearBlocks();
    // Too short, or never above the noise threshold: don't transcribe silence.
    if (samples.length < TARGET_RATE * MIN_SECONDS || peakOf(samples) < pctToRms(cfg.threshold)) {
      setState("idle");
      return;
    }
    transcribe(samples, false); // hold mode: fill the input, never auto-send
  }

  async function continuousToggle() {
    if (listening) {
      stopContinuous();
      return;
    }
    try {
      await startMic();
      listening = true;
      clearBlocks();
      setState("listening");
    } catch (e) {
      fail(e.message || "microphone unavailable");
    }
  }

  function stopContinuous() {
    listening = false;
    clearBlocks();
    setState("idle");
  }

  function dispose() {
    listening = false;
    clearBlocks();
    if (stream) {
      for (const t of stream.getTracks()) t.stop();
      stream = null;
    }
    if (proc) {
      proc.onaudioprocess = null;
      proc.disconnect();
      proc = null;
    }
    if (ctx) {
      ctx.close().catch(() => {});
      ctx = null;
    }
    analyser = null;
  }

  return {
    configure,
    holdStart,
    holdEnd,
    continuousToggle,
    dispose,
    rmsToPct,
    get listening() {
      return listening;
    },
    get state() {
      return stateName;
    },
    set onSpeechStart(fn) {
      hooks.speechStart = fn;
    },
    set onUtterance(fn) {
      hooks.utterance = fn;
    },
    set onStateChange(fn) {
      hooks.state = fn;
    },
    set onError(fn) {
      hooks.error = fn;
    },
    set onLevel(fn) {
      hooks.level = fn;
    },
  };
})();
