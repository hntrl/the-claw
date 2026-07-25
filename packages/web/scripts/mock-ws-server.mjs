import { WebSocketServer } from "ws";

const port = Number(process.env.PORT || 8787);
const wss = new WebSocketServer({ port });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const sequence = [
  { type: "state", state: "attract" },
  { type: "effect", effect: "voiceDetected" },
  { type: "state", state: "listening" },
  {
    type: "transcript",
    text: "grab the green capsule near the front left corner",
    isFinal: false,
  },
  {
    type: "transcript",
    text: "grab the green capsule near the front left corner",
    isFinal: true,
  },
  { type: "agent_step", step: "speech_to_text", status: "complete" },
  { type: "agent_step", step: "intent_parse", status: "complete" },
  { type: "effect", effect: "commandParsed" },
  { type: "state", state: "thinking" },
  { type: "agent_step", step: "target_select", status: "active" },
  { type: "target", label: "Green Capsule", confidence: 0.92 },
  { type: "agent_step", step: "target_select", status: "complete" },
  { type: "agent_step", step: "motion_plan", status: "active" },
  { type: "state", state: "moving" },
  { type: "claw_motion", direction: "right", speed: 0.8 },
  { type: "effect", effect: "moveStarted" },
  { type: "state", state: "dropping" },
  { type: "effect", effect: "dropStarted" },
  { type: "agent_step", step: "claw_execute", status: "active" },
  {
    type: "result",
    outcome: Math.random() > 0.35 ? "success" : "failure",
    label: "Green Capsule",
  },
  { type: "agent_step", step: "result_evaluate", status: "complete" },
];

const send = (payload) => {
  const serialized = JSON.stringify(payload);
  for (const client of wss.clients) {
    if (client.readyState === client.OPEN) {
      client.send(serialized);
    }
  }
};

const runLoop = async () => {
  while (true) {
    for (const event of sequence) {
      if (event.type === "result") {
        event.outcome = Math.random() > 0.35 ? "success" : "failure";
      }
      send(event);
      await sleep(event.type === "transcript" ? 950 : 1300);
    }
    await sleep(2400);
  }
};

wss.on("connection", (socket) => {
  socket.send(JSON.stringify({ type: "state", state: "attract" }));
});

console.log(`[mock-ws] listening on ws://localhost:${port}`);
runLoop().catch((err) => {
  console.error("[mock-ws] loop failed", err);
  process.exit(1);
});
