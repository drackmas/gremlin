/* stt.js — client-side speech capture for speech-to-text.
 *
 * Hold mode: press-and-hold the mic button; the whole window is transcribed
 * and placed in the input for edit/Send (never auto-sent).
 *
 * Continuous mode: tap the mic to start listening; an utterance starts when
 * the RMS level crosses the noise threshold (which also interrupts any
 * running reply via the onSpeechStart hook, so the user can barge in during
 * long tool loops) and ends after the configured silence duration. Each
 * utterance is transcribed and auto-sent, then listening resumes.
 *
 * Audio is captured in the browser, resampled to 16 kHz mono s16le, wrapped
 * in a minimal WAV header and POSTed to /api/stt/transcribe. The mic
 * permission is requested only on the first interaction with the mic button.
 */
const stt = (() => {
  const TARGET_RATE = 16000;
  const MIN_SAMPLES = TARGET_RATE * 0.2; // 200 ms — shorter than this is dropped

  let cfg = { enabled: false, mode: "hold", model: "base", threshold: 0.02, silence: 0.8 };

  let ctx = null;      // AudioContext
  let holdQueued = false;   // press arrived while the mic graph was being set up
  let holdReleased = false; // release arrived while the mic graph was being set up
  let stream = null;   // MediaStream (mic)
  let analyser = null; // AnalyserNode (RMS)
  let proc = null;     // ScriptProcessorNode (sample accumulation)
  let listening = false; // mic running (hold: while pressed; continuous: on)
  let recording = false; // accumulating an utterance
  let buf = new Float32Array(0);
  let silenceSince = 0;  // performance.now() when level dropped below threshold
  let dropUntil = 0;     // ignore samples until this time (TTS tail decay)
  let stateName = "idle"; // "idle" | "listening" | "recording" | "transcribing"

  const hooks = { speechStart: null, utterance: null, state: null, error: null };

  function configure(c) {
    Object.assign(cfg, c);
  }

  function setState(s) {
    stateName = s;
    if (hooks.state) hooks.state(s);
  }

  function fail(msg) {
    listening = false;
    recording = false;
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
    proc = ctx.createScriptProcessor(4096, 1, 1);
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
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    const rms = Math.sqrt(sum / data.length);
    if (recording && performance.now() >= dropUntil) {
      const nb = new Float32Array(buf.length + data.length);
      nb.set(buf);
      nb.set(data, buf.length);
      buf = nb;
    }
    if (cfg.mode !== "continuous") return;
    onRms(rms);
  }

  function onRms(rms) {
    const now = performance.now();
    if (!recording) {
      if (rms >= cfg.threshold) startUtterance();
      return;
    }
    if (rms >= cfg.threshold) {
      silenceSince = 0;
    } else if (!silenceSince) {
      silenceSince = now;
    } else if (now - silenceSince >= cfg.silence * 1000) {
      endUtterance();
    }
  }

  function startUtterance() {
    const wasSpeaking = typeof tts !== "undefined" && tts.playing;
    if (hooks.speechStart) hooks.speechStart(); // stop TTS + abort generation
    buf = new Float32Array(0);
    recording = true;
    silenceSince = 0;
    // If we just interrupted playback, drop its tail from the recording.
    dropUntil = wasSpeaking ? performance.now() + 250 : 0;
    setState("recording");
  }

  function endUtterance() {
    recording = false;
    silenceSince = 0;
    const samples = buf;
    buf = new Float32Array(0);
    if (samples.length < MIN_SAMPLES) {
      setState("listening");
      return;
    }
    transcribe(samples, true);
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

  function peakOf(samples) {
    let peak = 0;
    for (let i = 0; i < samples.length; i++) {
      const a = Math.abs(samples[i]);
      if (a > peak) peak = a;
    }
    return peak;
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
    recording = true;
    buf = new Float32Array(0);
    silenceSince = 0;
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
    recording = false;
    const samples = buf;
    buf = new Float32Array(0);
    // Too short, or never above the noise threshold: don't transcribe silence.
    if (samples.length < MIN_SAMPLES || peakOf(samples) < cfg.threshold) {
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
      recording = false;
      buf = new Float32Array(0);
      silenceSince = 0;
      setState("listening");
    } catch (e) {
      fail(e.message || "microphone unavailable");
    }
  }

  function stopContinuous() {
    listening = false;
    recording = false;
    buf = new Float32Array(0);
    setState("idle");
  }

  function dispose() {
    listening = false;
    recording = false;
    buf = new Float32Array(0);
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
  };
})();
