import { useEffect, useRef } from "react";
import type { BackgroundMode } from "../../types/display";

type AmbientLayerProps = {
  mode: BackgroundMode;
  intensity: number;
  accentColor: string;
};

type Point = {
  x: number;
  y: number;
};

type Curve = [Point, Point, Point, Point];

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

const cubicPoint = ([p0, p1, p2, p3]: Curve, t: number): Point => {
  const x =
    (1 - t) ** 3 * p0.x +
    3 * (1 - t) ** 2 * t * p1.x +
    3 * (1 - t) * t ** 2 * p2.x +
    t ** 3 * p3.x;
  const y =
    (1 - t) ** 3 * p0.y +
    3 * (1 - t) ** 2 * t * p1.y +
    3 * (1 - t) * t ** 2 * p2.y +
    t ** 3 * p3.y;

  return { x, y };
};

const hexToRgb = (hex: string): [number, number, number] => {
  const clean = hex.replace("#", "");
  const full = clean.length === 3
    ? clean
        .split("")
        .map((char) => `${char}${char}`)
        .join("")
    : clean;
  const asNumber = Number.parseInt(full, 16);
  return [(asNumber >> 16) & 255, (asNumber >> 8) & 255, asNumber & 255];
};

export const AmbientLayer = ({ mode, intensity, accentColor }: AmbientLayerProps) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const context = canvas.getContext("2d");
    if (!context) {
      return;
    }

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    const [ar, ag, ab] = hexToRgb(accentColor);

    let raf = 0;
    const drawCurve = (curve: Curve, color: string, width = 1, dash: number[] = []) => {
      context.save();
      context.strokeStyle = color;
      context.lineWidth = width;
      context.setLineDash(dash);
      context.beginPath();
      context.moveTo(curve[0].x, curve[0].y);
      context.bezierCurveTo(curve[1].x, curve[1].y, curve[2].x, curve[2].y, curve[3].x, curve[3].y);
      context.stroke();
      context.restore();
    };

    const drawNode = (point: Point, radius: number, color: string, fill = true) => {
      context.beginPath();
      context.arc(point.x, point.y, radius, 0, Math.PI * 2);
      if (fill) {
        context.fillStyle = color;
        context.fill();
      } else {
        context.strokeStyle = color;
        context.lineWidth = 1;
        context.stroke();
      }
    };

    const draw = (timeMs: number) => {
      const t = timeMs / 1000;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const sweep = (t * (0.09 + intensity * 0.08)) % 1;
      const jitter = mode === "glitch" ? Math.sin(t * 28) * 2 : 0;

      context.clearRect(0, 0, width, height);
      context.fillStyle = "#f7f7f2";
      context.fillRect(0, 0, width, height);

      const haze = context.createRadialGradient(width * 0.5, height * 0.46, 0, width * 0.5, height * 0.46, height * 0.7);
      haze.addColorStop(0, `rgba(${ar}, ${ag}, ${ab}, ${0.08 + intensity * 0.05})`);
      haze.addColorStop(0.42, "rgba(142, 200, 255, 0.04)");
      haze.addColorStop(1, "rgba(0, 0, 0, 0)");
      context.fillStyle = haze;
      context.fillRect(0, 0, width, height);

      const left: Point = { x: width * 0.08 + jitter, y: height * 0.6 };
      const right: Point = { x: width * 0.92 + jitter, y: height * 0.6 };
      const bottom: Point = { x: width * 0.5 + jitter, y: height * 0.88 };
      const top: Point = { x: width * 0.5 + jitter, y: height * 0.13 };
      const mid: Point = { x: width * 0.5 + jitter, y: height * 0.6 };
      const upper: Point = { x: width * 0.5 + jitter, y: height * 0.33 };
      const lower: Point = { x: width * 0.5 + jitter, y: height * 0.78 };
      const line = "rgba(0, 0, 0, 0.7)";
      const dim = "rgba(0, 0, 0, 0.18)";
      const dotted = "rgba(0, 0, 0, 0.36)";
      const active = mode === "celebration"
        ? "rgba(184, 132, 18, 0.92)"
        : mode === "glitch"
          ? "rgba(190, 42, 52, 0.86)"
          : `rgba(${Math.max(0, ar - 50)}, ${Math.max(0, ag - 40)}, ${Math.max(0, ab - 20)}, 0.9)`;

      context.globalCompositeOperation = "source-over";
      context.shadowColor = "rgba(83, 118, 174, 0.18)";
      context.shadowBlur = 5;

      context.strokeStyle = dim;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(0, mid.y);
      context.lineTo(width, mid.y);
      context.moveTo(mid.x, height * 0.05);
      context.lineTo(mid.x, height * 0.95);
      context.stroke();

      const curves: Curve[] = [
        [left, { x: width * 0.24, y: height * 0.32 }, { x: width * 0.34, y: height * 0.17 }, top],
        [left, { x: width * 0.27, y: height * 0.44 }, { x: width * 0.38, y: height * 0.32 }, upper],
        [left, { x: width * 0.25, y: height * 0.54 }, { x: width * 0.38, y: height * 0.52 }, mid],
        [left, { x: width * 0.25, y: height * 0.68 }, { x: width * 0.38, y: height * 0.73 }, lower],
        [left, { x: width * 0.2, y: height * 0.82 }, { x: width * 0.35, y: height * 0.92 }, bottom],
        [right, { x: width * 0.76, y: height * 0.32 }, { x: width * 0.66, y: height * 0.17 }, top],
        [right, { x: width * 0.73, y: height * 0.44 }, { x: width * 0.62, y: height * 0.32 }, upper],
        [right, { x: width * 0.75, y: height * 0.54 }, { x: width * 0.62, y: height * 0.52 }, mid],
        [right, { x: width * 0.75, y: height * 0.68 }, { x: width * 0.62, y: height * 0.73 }, lower],
        [right, { x: width * 0.8, y: height * 0.82 }, { x: width * 0.65, y: height * 0.92 }, bottom],
      ];

      curves.forEach((curve, index) => {
        const isDotted = index % 2 === 1;
        drawCurve(curve, isDotted ? dotted : line, isDotted ? 0.8 : 1, isDotted ? [1, 6] : []);
      });

      const activeCurveIndex =
        mode === "audioGrid" ? 2 :
        mode === "thinkingGraph" ? Math.floor(t * 1.7) % curves.length :
        mode === "motionVectors" ? 7 :
        mode === "celebration" ? 4 :
        mode === "glitch" ? 8 :
        0;
      const activeCurve = curves[activeCurveIndex];
      drawCurve(activeCurve, active, 1.6);

      const packet = cubicPoint(activeCurve, sweep);
      drawNode(packet, 4 + intensity * 2, active);
      drawNode(cubicPoint(activeCurve, (sweep + 0.12) % 1), 2.2, `rgba(${ar}, ${ag}, ${ab}, 0.52)`);

      for (const node of [left, right, bottom, top, mid, upper, lower]) {
        drawNode(node, node === left || node === right || node === bottom ? 5 : 4, "rgba(0, 0, 0, 0.82)");
      }

      if (mode === "audioGrid") {
        context.strokeStyle = `rgba(${ar}, ${ag}, ${ab}, 0.58)`;
        context.lineWidth = 1;
        context.beginPath();
        for (let y = height * 0.2; y < height * 0.74; y += 13) {
          const wave = Math.sin(y * 0.05 + t * 4) * (5 + intensity * 9);
          context.moveTo(width * 0.12, y);
          context.lineTo(width * 0.12 + wave, y + 4);
        }
        context.stroke();
      }

      if (mode === "motionVectors") {
        const dropY = lerp(height * 0.2, height * 0.76, sweep);
        context.strokeStyle = active;
        context.lineWidth = 1.4;
        context.beginPath();
        context.moveTo(width * 0.5, height * 0.18);
        context.lineTo(width * 0.5, dropY);
        context.stroke();
        drawNode({ x: width * 0.5, y: dropY }, 3.5, active);
      }

      if (mode === "glitch") {
        context.shadowBlur = 0;
        context.fillStyle = "rgba(190, 42, 52, 0.1)";
        for (let i = 0; i < 5; i += 1) {
          context.fillRect(Math.sin(t * 19 + i) * 18, (i * 113 + t * 90) % height, width, 3);
        }
      }

      context.globalCompositeOperation = "source-over";
      context.shadowBlur = 0;
      raf = window.requestAnimationFrame(draw);
    };

    raf = window.requestAnimationFrame(draw);

    return () => {
      window.cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [accentColor, intensity, mode]);

  return <canvas className="layer ambient-layer" ref={canvasRef} aria-hidden="true" />;
};
