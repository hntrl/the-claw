import { useCallback, useEffect, useRef, useState } from "react";

const TARGET_SAMPLE_RATE = 24_000;
const PROCESSOR_BUFFER_SIZE = 2_048;

type ClientMicControls = {
  muted: boolean;
  supported: boolean;
  error?: string;
  toggleMuted: () => void;
};

const resample = (
  input: Float32Array,
  inputSampleRate: number,
  targetSampleRate: number,
): Float32Array => {
  if (inputSampleRate === targetSampleRate) {
    return input;
  }

  const outputLength = Math.max(
    1,
    Math.round((input.length * targetSampleRate) / inputSampleRate),
  );
  const output = new Float32Array(outputLength);

  for (let index = 0; index < outputLength; index += 1) {
    const sourcePosition = (index * inputSampleRate) / targetSampleRate;
    const leftIndex = Math.floor(sourcePosition);
    const rightIndex = Math.min(leftIndex + 1, input.length - 1);
    const blend = sourcePosition - leftIndex;
    output[index] = input[leftIndex] * (1 - blend) + input[rightIndex] * blend;
  }

  return output;
};

const float32ToPcm16 = (input: Float32Array): Uint8Array => {
  const output = new Uint8Array(input.length * 2);
  const view = new DataView(output.buffer);

  for (let index = 0; index < input.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, input[index]));
    const int16 = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    view.setInt16(index * 2, int16, true);
  }

  return output;
};

export const useClientMicStream = (
  sendAudioChunk: (chunk: Uint8Array) => boolean,
): ClientMicControls => {
  const [muted, setMuted] = useState(true);
  const [error, setError] = useState<string | undefined>(undefined);
  const [supported] = useState(
    typeof window !== "undefined" &&
      typeof navigator !== "undefined" &&
      Boolean(navigator.mediaDevices?.getUserMedia),
  );

  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sinkRef = useRef<GainNode | null>(null);

  const stopCapture = useCallback(async () => {
    processorRef.current?.disconnect();
    if (processorRef.current) {
      processorRef.current.onaudioprocess = null;
    }
    sourceRef.current?.disconnect();
    sinkRef.current?.disconnect();

    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) {
        track.stop();
      }
    }

    const context = contextRef.current;
    if (context && context.state !== "closed") {
      await context.close();
    }

    processorRef.current = null;
    sourceRef.current = null;
    sinkRef.current = null;
    streamRef.current = null;
    contextRef.current = null;
  }, []);

  const startCapture = useCallback(async (): Promise<boolean> => {
    if (!supported) {
      return false;
    }

    try {
      setError(undefined);
      await stopCapture();

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      const AudioContextConstructor =
        window.AudioContext ||
        (window as Window & { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!AudioContextConstructor) {
        throw new Error("AudioContext is not available");
      }

      const context = new AudioContextConstructor({
        sampleRate: TARGET_SAMPLE_RATE,
        latencyHint: "interactive",
      });
      if (context.state === "suspended") {
        await context.resume();
      }

      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(
        PROCESSOR_BUFFER_SIZE,
        1,
        1,
      );
      const sink = context.createGain();
      sink.gain.value = 0;

      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        if (!input.length) {
          return;
        }

        const resampled = resample(
          input,
          context.sampleRate,
          TARGET_SAMPLE_RATE,
        );
        const chunk = float32ToPcm16(resampled);
        sendAudioChunk(chunk);
      };

      source.connect(processor);
      processor.connect(sink);
      sink.connect(context.destination);

      streamRef.current = stream;
      contextRef.current = context;
      sourceRef.current = source;
      processorRef.current = processor;
      sinkRef.current = sink;
      return true;
    } catch (captureError) {
      setError(
        captureError instanceof Error
          ? captureError.message
          : "Microphone capture failed",
      );
      return false;
    }
  }, [sendAudioChunk, stopCapture, supported]);

  useEffect(() => {
    if (muted) {
      void stopCapture();
      return;
    }

    let cancelled = false;
    void (async () => {
      const started = await startCapture();
      if (!started && !cancelled) {
        setMuted(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [muted, startCapture, stopCapture]);

  useEffect(() => {
    return () => {
      void stopCapture();
    };
  }, [stopCapture]);

  const toggleMuted = useCallback(() => {
    if (!supported) {
      return;
    }
    setMuted((current) => !current);
  }, [supported]);

  return {
    muted,
    supported,
    error,
    toggleMuted,
  };
};
