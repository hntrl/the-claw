const listenBtn = document.getElementById("listenBtn");
const forceRecordBtn = document.getElementById("forceRecordBtn");
const micStatus = document.getElementById("micStatus");
const logEl = document.getElementById("log");
const stateView = document.getElementById("stateView");
const estopBtn = document.getElementById("estopBtn");
const resetBtn = document.getElementById("resetBtn");
const refreshStateBtn = document.getElementById("refreshStateBtn");
const manualInput = document.getElementById("manualInput");
const sendBtn = document.getElementById("sendBtn");
const speakToggleBtn = document.getElementById("speakToggleBtn");
const clearBtn = document.getElementById("clearBtn");

const sessionId = `web-${Math.random().toString(36).slice(2, 9)}`;

let speakEnabled = true;
let listening = false;
let processingTurn = false;

let stream = null;
let audioContext = null;
let analyser = null;
let source = null;
let mediaRecorder = null;
let vadTimer = null;
let chunks = [];
let recording = false;
let speechHits = 0;
let silenceHits = 0;
let recordingStartedAt = 0;
let pttHeld = false;
let pttForcedRecording = false;
let suppressUntil = 0;
let activeAudio = null;

const VAD_INTERVAL_MS = 100;
const VAD_START_THRESHOLD = 0.012;
const VAD_STOP_THRESHOLD = 0.008;
const VAD_MIN_SPEECH_HITS = 3;
const VAD_SILENCE_HITS_TO_STOP = 9;
const MIN_UTTERANCE_MS = 450;
const POST_TTS_SUPPRESS_MS = 900;
const MAX_UTTERANCE_MS = 7000;
const NOISE_FLOOR_ALPHA = 0.08;
const NOISE_FLOOR_MULTIPLIER = 2.8;
const PUSH_TO_TALK_KEY = "F7";
let noiseFloor = 0.004;

function addLog(label, content) {
  const entry = document.createElement("div");
  entry.className = "entry";
  entry.innerHTML = `<div class="label">${label}</div><div>${content}</div>`;
  logEl.prepend(entry);
}

function setMicStatus(text) {
  micStatus.textContent = `Mic: ${text}`;
}

async function speak(text) {
  if (!speakEnabled) return;
  try {
    const resp = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) {
      throw new Error(await resp.text());
    }
    const blob = await resp.blob();
    if (!blob.size) return;

    if (activeAudio) {
      activeAudio.pause();
      activeAudio = null;
    }
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    activeAudio = audio;
    audio.onplay = () => {
      suppressUntil = Date.now() + POST_TTS_SUPPRESS_MS;
    };
    await audio.play();
    await new Promise((resolve) => {
      audio.onended = () => {
        URL.revokeObjectURL(url);
        if (activeAudio === audio) activeAudio = null;
        resolve();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        if (activeAudio === audio) activeAudio = null;
        resolve();
      };
    });
  } catch (err) {
    addLog("TTS Error", String(err));
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel();
      await new Promise((resolve) => {
        const utter = new SpeechSynthesisUtterance(text);
        utter.onstart = () => {
          suppressUntil = Date.now() + POST_TTS_SUPPRESS_MS;
        };
        utter.onend = () => resolve();
        utter.onerror = () => resolve();
        window.speechSynthesis.speak(utter);
      });
    }
  }
}

async function fetchState() {
  try {
    const resp = await fetch("/api/state");
    const data = await resp.json();
    stateView.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    stateView.textContent = `State fetch failed: ${err}`;
  }
}

async function sendTurn(text) {
  const cleaned = (text || "").trim();
  if (!cleaned || processingTurn) return;
  processingTurn = true;
  addLog("You", cleaned);
  setMicStatus("processing");
  try {
    const resp = await fetch("/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: cleaned, session_id: sessionId }),
    });
    if (!resp.ok) {
      addLog("Error", await resp.text());
      return;
    }
    const data = await resp.json();
    addLog("Agent", data.reply);
    await speak(data.reply);
    if (Array.isArray(data.tool_events) && data.tool_events.length) {
      addLog("Tools", `<pre>${JSON.stringify(data.tool_events, null, 2)}</pre>`);
      fetchState();
    }
  } catch (err) {
    addLog("Error", String(err));
  } finally {
    processingTurn = false;
    setMicStatus(listening ? "listening" : "idle");
  }
}

function getRms() {
  if (!analyser) return 0;
  const data = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i += 1) sum += data[i] * data[i];
  return Math.sqrt(sum / data.length);
}

async function transcribeBlob(blob) {
  const form = new FormData();
  form.append("audio", blob, "mic.webm");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  const resp = await fetch("/api/transcribe", { method: "POST", body: form, signal: controller.signal })
    .finally(() => clearTimeout(timer));
  if (!resp.ok) {
    throw new Error(await resp.text());
  }
  const data = await resp.json();
  return (data.text || "").trim();
}

function startRecording({ pushToTalk = false } = {}) {
  if (!mediaRecorder || mediaRecorder.state !== "inactive" || processingTurn) return false;
  chunks = [];
  speechHits = 0;
  silenceHits = 0;
  recording = true;
  pttForcedRecording = pushToTalk;
  recordingStartedAt = Date.now();
  mediaRecorder.start();
  setMicStatus(pushToTalk ? "recording (push-to-talk)" : "recording speech");
  return true;
}

async function stopRecordingAndProcess() {
  if (!recording || !mediaRecorder || mediaRecorder.state !== "recording") return;
  recording = false;
  setMicStatus("finalizing audio");
  mediaRecorder.stop();
}

function runVadTick() {
  if (!listening || processingTurn) return;
  if (Date.now() < suppressUntil) return;
  if (pttHeld) return;

  const rms = getRms();
  if (!recording) {
    noiseFloor = (1 - NOISE_FLOOR_ALPHA) * noiseFloor + NOISE_FLOOR_ALPHA * rms;
  }
  const startThreshold = Math.max(VAD_START_THRESHOLD, noiseFloor * NOISE_FLOOR_MULTIPLIER);
  const stopThreshold = Math.max(VAD_STOP_THRESHOLD, startThreshold * 0.55);

  if (!recording) {
    setMicStatus(`listening (rms ${rms.toFixed(4)}, thr ${startThreshold.toFixed(4)})`);
    if (rms >= startThreshold) speechHits += 1;
    else speechHits = Math.max(0, speechHits - 1);
    if (speechHits >= VAD_MIN_SPEECH_HITS) startRecording();
    return;
  }

  const elapsed = Date.now() - recordingStartedAt;
  if (rms < stopThreshold) silenceHits += 1;
  else silenceHits = 0;

  if (silenceHits >= VAD_SILENCE_HITS_TO_STOP || elapsed >= MAX_UTTERANCE_MS) {
    stopRecordingAndProcess();
  }
}

async function initMic() {
  if (stream) return;
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  audioContext = new AudioContext();
  if (audioContext.state === "suspended") {
    await audioContext.resume();
  }
  source = audioContext.createMediaStreamSource(stream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);

  const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : "audio/webm";
  mediaRecorder = new MediaRecorder(stream, { mimeType });
  mediaRecorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  };
  mediaRecorder.onstop = async () => {
    pttForcedRecording = false;
    const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
    chunks = [];
    if (blob.size < 1000 || Date.now() - recordingStartedAt < MIN_UTTERANCE_MS) {
      setMicStatus(listening ? "listening" : "idle");
      return;
    }
    try {
      setMicStatus("transcribing");
      const text = await transcribeBlob(blob);
      if (text) {
        await sendTurn(text);
      } else {
        addLog("System", "No speech text detected. Listening again.");
      }
    } catch (err) {
      addLog("Transcription Error", String(err));
      setMicStatus("transcription failed");
    } finally {
      if (!processingTurn) {
        setMicStatus(listening ? "listening" : "idle");
      }
    }
  };
}

async function startListening() {
  await initMic();
  listening = true;
  setMicStatus("listening");
  listenBtn.textContent = "Stop Listening";
  listenBtn.classList.add("active");
  if (vadTimer) clearInterval(vadTimer);
  vadTimer = setInterval(runVadTick, VAD_INTERVAL_MS);
}

function stopListening() {
  listening = false;
  pttHeld = false;
  pttForcedRecording = false;
  listenBtn.textContent = "Start Listening";
  listenBtn.classList.remove("active");
  if (vadTimer) {
    clearInterval(vadTimer);
    vadTimer = null;
  }
  if (recording) stopRecordingAndProcess();
  setMicStatus("idle");
}

async function startPushToTalk() {
  if (processingTurn) return;
  if (recording && !pttForcedRecording) return;
  await initMic();
  pttHeld = true;
  if (!recording) startRecording({ pushToTalk: true });
}

function stopPushToTalk() {
  if (!pttHeld) return;
  pttHeld = false;
  if (recording && pttForcedRecording) stopRecordingAndProcess();
}

listenBtn.addEventListener("click", async () => {
  try {
    if (!listening) await startListening();
    else stopListening();
  } catch (err) {
    addLog("Mic Error", `Could not start microphone: ${err}`);
    setMicStatus("mic unavailable");
  }
});

forceRecordBtn.addEventListener("click", async () => {
  try {
    if (!listening) {
      await startListening();
    }
    if (!recording) {
      startRecording();
      setTimeout(() => {
        if (recording) stopRecordingAndProcess();
      }, 2500);
    }
  } catch (err) {
    addLog("Mic Error", `Could not force record: ${err}`);
  }
});

speakToggleBtn.addEventListener("click", () => {
  speakEnabled = !speakEnabled;
  speakToggleBtn.textContent = `Voice Reply: ${speakEnabled ? "On" : "Off"}`;
  if (!speakEnabled) {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (activeAudio) {
      activeAudio.pause();
      activeAudio = null;
    }
  }
});

clearBtn.addEventListener("click", () => {
  logEl.innerHTML = "";
});

sendBtn.addEventListener("click", () => {
  sendTurn(manualInput.value);
  manualInput.value = "";
});

manualInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    sendTurn(manualInput.value);
    manualInput.value = "";
  }
});

window.addEventListener("keydown", async (event) => {
  if (event.key !== PUSH_TO_TALK_KEY) return;
  event.preventDefault();
  if (event.repeat) return;
  try {
    await startPushToTalk();
  } catch (err) {
    pttHeld = false;
    pttForcedRecording = false;
    addLog("Mic Error", `Could not start push-to-talk: ${err}`);
    setMicStatus("mic unavailable");
  }
});

window.addEventListener("keyup", (event) => {
  if (event.key !== PUSH_TO_TALK_KEY) return;
  event.preventDefault();
  stopPushToTalk();
});

window.addEventListener("blur", () => {
  stopPushToTalk();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPushToTalk();
});

estopBtn.addEventListener("click", async () => {
  await fetch("/api/emergency-stop", { method: "POST" });
  addLog("System", "Emergency stop sent.");
  fetchState();
});

resetBtn.addEventListener("click", async () => {
  await fetch("/api/reset-emergency", { method: "POST" });
  addLog("System", "Emergency reset sent.");
  fetchState();
});

refreshStateBtn.addEventListener("click", fetchState);

fetchState();
setInterval(fetchState, 3000);
