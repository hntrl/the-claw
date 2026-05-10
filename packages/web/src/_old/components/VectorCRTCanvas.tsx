import { useEffect, useMemo, useRef, useState } from "react";
import interrupt26LogoSrc from "../../sprites/interrupt26.png";
import { fallbackSpriteMood, spriteByMood } from "../state/spriteAssets";
import { useDisplayStore } from "../state/displayStore";
import type { KeyboardTextOverlay } from "../hooks/useKeyboardTextInput";
import { visualStateMap } from "../state/visualStateMap";
import type { BackgroundMode, EventEffect, SpriteMood } from "../types/display";

type VectorCRTCanvasProps = {
  mood?: SpriteMood;
  keyboardOverlay?: KeyboardTextOverlay;
};

type Point = {
  x: number;
  y: number;
};

type TrimmedImage = {
  image: HTMLImageElement;
  sx: number;
  sy: number;
  sw: number;
  sh: number;
};

const ATTRACT_BLINK_INTERVAL_MS = 5200;
const ATTRACT_BLINK_DURATION_MS = 120;

const drawCurvedText = (
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  width: number,
  height: number,
  align: CanvasTextAlign = "left",
) => {
  const previousAlign = context.textAlign;
  context.textAlign = "left";

  const totalWidth = context.measureText(text).width;
  const startX = align === "right" ? x - totalWidth : align === "center" ? x - totalWidth / 2 : x;
  let cursorX = startX;

  for (const character of text) {
    const characterWidth = context.measureText(character).width;
    const centerX = cursorX + characterWidth / 2;
    const curveY = curvedHeaderY(centerX, y, width, height);

    context.fillText(character, cursorX, curveY);
    cursorX += characterWidth;
  }

  context.textAlign = previousAlign;
};

const curvedHeaderY = (x: number, y: number, width: number, height: number) => {
  const nx = x / width * 2 - 1;
  return y + (Math.abs(nx) ** 2) * height * 0.012;
};

const drawCurvedHorizontalRule = (
  context: CanvasRenderingContext2D,
  y: number,
  width: number,
  height: number,
) => {
  const segments = 80;

  context.moveTo(0, curvedHeaderY(0, y, width, height));

  for (let index = 1; index <= segments; index += 1) {
    const x = width * (index / segments);
    context.lineTo(x, curvedHeaderY(x, y, width, height));
  }
};

const drawCurvedImage = (
  context: CanvasRenderingContext2D,
  image: TrimmedImage,
  x: number,
  y: number,
  targetWidth: number,
  targetHeight: number,
  screenWidth: number,
  screenHeight: number,
) => {
  const slices = Math.max(28, Math.round(targetWidth / 10));
  const overlap = Math.max(0.5, targetWidth * 0.002);

  for (let index = 0; index < slices; index += 1) {
    const progress = index / slices;
    const nextProgress = (index + 1) / slices;
    const sx = image.sx + image.sw * progress;
    const sw = image.sw * (nextProgress - progress);
    const dx = x + targetWidth * progress;
    const dw = targetWidth * (nextProgress - progress);
    const centerX = dx + dw / 2;
    const dy = curvedHeaderY(centerX, y, screenWidth, screenHeight);

    context.drawImage(
      image.image,
      sx,
      image.sy,
      sw,
      image.sh,
      dx - overlap / 2,
      dy,
      dw + overlap,
      targetHeight,
    );
  }
};

const drawHeaderMarquee = (
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  timeSeconds: number,
  fontFamily: string,
  logoImage: TrimmedImage | null,
) => {
  const top = height * 0.064;
  const bottom = height * 0.101;
  const baseline = height * 0.091;
  const label = "INTERRUPT 26";

  context.save();
  context.beginPath();
  context.rect(0, top, width, bottom - top);
  context.clip();

  context.fillStyle = "#f7f7f2";
  context.fillRect(0, top, width, bottom - top);

  context.font = `${Math.round(height * 0.023)}px ${fontFamily}`;
  context.fillStyle = "rgba(0,0,0,0.72)";
  const markHeight = (bottom - top) * 0.88;
  const markWidth = logoImage ? markHeight * (logoImage.sw / logoImage.sh) : context.measureText(label).width;
  const markY = top + (bottom - top - markHeight) / 2;
  const gap = width * 0.016;
  const step = markWidth + gap;
  const offset = -((timeSeconds * width * 0.028) % step);

  for (let x = offset - step; x < width + step; x += step) {
    if (logoImage) {
      drawCurvedImage(context, logoImage, x, markY, markWidth, markHeight, width, height);
    } else {
      drawCurvedText(context, label, x, baseline, width, height);
    }
  }

  context.restore();

  context.save();
  context.strokeStyle = "rgba(0,0,0,0.26)";
  context.lineWidth = Math.max(1, width * 0.0012);
  context.beginPath();
  drawCurvedHorizontalRule(context, top, width, height);
  drawCurvedHorizontalRule(context, bottom, width, height);
  context.stroke();
  context.restore();
};

const fieldParamsByMode: Record<BackgroundMode, {
  columns: number;
  rows: number;
  speed: number;
  jitter: number;
  holeX: number;
  holeY: number;
  alpha: number;
}> = {
  idleGrid: { columns: 12, rows: 20, speed: 0.18, jitter: 0, holeX: 0.32, holeY: 0.23, alpha: 0.7 },
  audioGrid: { columns: 16, rows: 25, speed: 0.58, jitter: 0, holeX: 0.34, holeY: 0.24, alpha: 0.86 },
  thinkingGraph: { columns: 10, rows: 17, speed: 0.42, jitter: 0, holeX: 0.35, holeY: 0.24, alpha: 0.88 },
  motionVectors: { columns: 11, rows: 22, speed: 1.18, jitter: 0.02, holeX: 0.36, holeY: 0.25, alpha: 0.94 },
  celebration: { columns: 10, rows: 17, speed: 0.86, jitter: 0, holeX: 0.32, holeY: 0.23, alpha: 0.9 },
  glitch: { columns: 13, rows: 22, speed: 0.9, jitter: 0.14, holeX: 0.34, holeY: 0.24, alpha: 0.94 },
};

const seededNoise = (x: number, y: number) => {
  const value = Math.sin(x * 127.1 + y * 311.7) * 43758.5453123;
  return value - Math.floor(value);
};

const colorWithAlpha = (hex: string, alpha: number) => {
  const value = hex.replace("#", "");
  const expanded = value.length === 3 ? value.split("").map((part) => part + part).join("") : value;
  const parsed = Number.parseInt(expanded, 16);
  if (Number.isNaN(parsed)) {
    return `rgba(57, 105, 164, ${alpha})`;
  }

  const r = (parsed >> 16) & 255;
  const g = (parsed >> 8) & 255;
  const b = parsed & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
};

const drawBracketMark = (context: CanvasRenderingContext2D, x: number, y: number, size: number) => {
  const arm = size * 0.36;
  const half = size * 0.5;

  context.beginPath();
  context.moveTo(x - half, y - half + arm);
  context.lineTo(x - half, y - half);
  context.lineTo(x - half + arm, y - half);
  context.moveTo(x + half - arm, y - half);
  context.lineTo(x + half, y - half);
  context.lineTo(x + half, y - half + arm);
  context.moveTo(x + half, y + half - arm);
  context.lineTo(x + half, y + half);
  context.lineTo(x + half - arm, y + half);
  context.moveTo(x - half + arm, y + half);
  context.lineTo(x - half, y + half);
  context.lineTo(x - half, y + half - arm);
  context.stroke();
};

const drawReticleMark = (context: CanvasRenderingContext2D, x: number, y: number, size: number, phase: number) => {
  const radius = size * (0.18 + 0.06 * Math.sin(phase));
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.moveTo(x - size * 0.42, y);
  context.lineTo(x - size * 0.22, y);
  context.moveTo(x + size * 0.22, y);
  context.lineTo(x + size * 0.42, y);
  context.moveTo(x, y - size * 0.42);
  context.lineTo(x, y - size * 0.22);
  context.moveTo(x, y + size * 0.22);
  context.lineTo(x, y + size * 0.42);
  context.stroke();
};

const drawChevronMark = (context: CanvasRenderingContext2D, x: number, y: number, size: number, direction: number) => {
  const angle = direction * Math.PI * 0.5;
  context.save();
  context.translate(x, y);
  context.rotate(angle);
  context.beginPath();
  context.moveTo(-size * 0.24, -size * 0.34);
  context.lineTo(size * 0.24, 0);
  context.lineTo(-size * 0.24, size * 0.34);
  context.stroke();
  context.restore();
};

const drawPrizeMark = (context: CanvasRenderingContext2D, x: number, y: number, size: number) => {
  const box = size * 0.44;
  context.strokeRect(x - box / 2, y - box / 2, box, box);
  context.beginPath();
  context.moveTo(x - box / 2, y);
  context.lineTo(x + box / 2, y);
  context.moveTo(x, y - box / 2);
  context.lineTo(x, y + box / 2);
  context.stroke();
};

const drawSignalField = (
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  timeSeconds: number,
  mode: BackgroundMode,
  intensity: number,
  accentColor: string,
) => {
  const params = fieldParamsByMode[mode];
  const cellW = width / params.columns;
  const cellH = height / params.rows;
  const drift = (timeSeconds * params.speed * cellH * 0.42) % cellH;
  const center: Point = { x: width * 0.5, y: height * 0.5 };
  const primary = mode === "celebration" ? "rgba(196, 139, 29, 0.94)" : colorWithAlpha(accentColor, 0.66 + intensity * 0.22);
  const secondary =
    mode === "celebration" ? "rgba(143, 92, 12, 0.62)" :
    mode === "glitch" ? "rgba(190, 42, 52, 0.7)" :
    "rgba(0, 0, 0, 0.5)";
  const faint = mode === "celebration" ? "rgba(184, 132, 18, 0.42)" : "rgba(0, 0, 0, 0.28)";

  context.save();
  context.lineCap = "round";
  context.lineJoin = "round";
  context.lineWidth = Math.max(1.35, width * 0.00175);

  for (let row = -1; row <= params.rows + 1; row += 1) {
    for (let column = -1; column <= params.columns + 1; column += 1) {
      const seed = seededNoise(column, row);
      const phase = timeSeconds * params.speed * (0.9 + seed) + seed * Math.PI * 2;
      const isThinking = mode === "thinkingGraph";
      const stagger = isThinking ? 0 : (row % 2) * cellW * 0.32;
      const noiseOffset = isThinking ? 0 : (seed - 0.5) * Math.min(cellW, cellH) * 0.26;
      const glitchOffset = params.jitter * Math.sin(phase * 9) * width;
      const thinkingSway = isThinking ? Math.sin(timeSeconds * 0.5 + row * 0.18) * cellW * 0.12 : 0;
      const x = column * cellW + stagger + cellW * 0.5 + thinkingSway + (isThinking ? 0 : Math.sin(phase) * noiseOffset) + glitchOffset;
      const y = row * cellH + cellH * 0.5 + (isThinking ? 0 : drift) + Math.cos(phase * 0.7) * noiseOffset;
      const dx = (x - center.x) / (width * params.holeX);
      const dy = (y - center.y) / (height * params.holeY);
      const holeDistance = dx * dx + dy * dy;
      const holeFade = Math.max(0, Math.min(1, (holeDistance - 0.5) / 0.58));
      const edgeFade = Math.max(0.42, Math.min(1, (Math.abs(x - center.x) / (width * 0.5)) ** 0.45));
      const alpha = params.alpha * holeFade * edgeFade * (0.72 + 0.28 * Math.sin(phase) ** 2);

      if (alpha < 0.035) {
        continue;
      }

      const size = Math.min(cellW, cellH) * (0.52 + seed * 0.22 + intensity * 0.12);
      context.globalAlpha = alpha;
      context.strokeStyle = seed > 0.68 ? primary : secondary;
      context.shadowBlur = 0;

      if (mode === "audioGrid") {
        const bar = size * (0.18 + 0.5 * Math.sin(phase * 2.5) ** 2);
        context.beginPath();
        context.moveTo(x, y - bar);
        context.lineTo(x, y + bar);
        context.stroke();
        if (seed > 0.72) {
          drawReticleMark(context, x, y, size, phase);
        }
      } else if (mode === "thinkingGraph") {
        if (seed > 0.22) {
          drawReticleMark(context, x, y, size, phase);
        }
        if (seed > 0.72) {
          context.strokeStyle = faint;
          context.beginPath();
          context.moveTo(x, y);
          context.lineTo(x + Math.cos(phase) * cellW * 0.86, y + Math.sin(phase * 0.8) * cellH * 0.72);
          context.stroke();
        }
      } else if (mode === "motionVectors") {
        drawChevronMark(context, x, y, size, Math.floor(seed * 4));
        if (seed > 0.66) {
          context.beginPath();
          context.moveTo(x - cellW * 0.28, y);
          context.lineTo(x + cellW * 0.28, y);
          context.stroke();
        }
      } else if (mode === "celebration") {
        if (seed > 0.48) {
          drawPrizeMark(context, x, y, size);
        } else {
          context.beginPath();
          context.moveTo(x - size * 0.34, y);
          context.lineTo(x + size * 0.34, y);
          context.moveTo(x, y - size * 0.34);
          context.lineTo(x, y + size * 0.34);
          context.stroke();
        }
      } else if (mode === "glitch") {
        context.strokeStyle = seed > 0.48 ? "rgba(190, 42, 52, 0.5)" : "rgba(0, 0, 0, 0.3)";
        context.beginPath();
        context.moveTo(x - size * 0.42, y);
        context.lineTo(x + size * (0.15 + seed * 0.55), y);
        context.stroke();
        if (seed > 0.72) {
          drawBracketMark(context, x + glitchOffset * 0.2, y, size * 0.72);
        }
      } else if (seed > 0.5) {
        drawBracketMark(context, x, y, size * 0.78);
      } else {
        drawPrizeMark(context, x, y, size * 0.72);
      }
    }
  }

  const sweepY = height * (0.17 + ((timeSeconds * params.speed * 0.12) % 0.68));
  context.globalAlpha = mode === "celebration" ? 0.48 : 0.2 + intensity * 0.15;
  context.strokeStyle = primary;
  context.shadowColor = colorWithAlpha(accentColor, 0.45);
  context.shadowBlur = width * 0.008;
  context.lineWidth = Math.max(1.4, width * 0.002);
  context.beginPath();
  context.moveTo(width * 0.08, sweepY);
  context.lineTo(width * 0.92, sweepY);
  context.stroke();

  if (mode === "motionVectors" || mode === "celebration" || mode === "glitch") {
    const ringRadius = width * (0.25 + Math.sin(timeSeconds * params.speed) * 0.018);
    context.globalAlpha = mode === "celebration" ? 0.32 : mode === "glitch" ? 0.12 : 0.18;
    context.strokeStyle = primary;
    context.beginPath();
    context.arc(center.x, center.y, ringRadius, -Math.PI * 0.14, Math.PI * 1.14);
    context.stroke();
  }

  context.restore();
};

const drawBaseBackground = (
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  _mode: BackgroundMode,
) => {
  context.fillStyle = "#f7f7f2";
  context.fillRect(0, 0, width, height);
};

const drawWarped = (
  context: CanvasRenderingContext2D,
  source: HTMLCanvasElement,
  width: number,
  height: number,
) => {
  const stripHeight = Math.max(1, Math.round(height / 720));

  const warpY = (sourceY: number) => {
    const ny = sourceY / height * 2 - 1;
    const rowCurve = Math.abs(ny) ** 1.8;
    return sourceY + Math.sign(ny) * rowCurve * height * 0.026;
  };

  context.clearRect(0, 0, width, height);
  context.imageSmoothingEnabled = true;

  for (let y = 0; y < height; y += stripHeight) {
    const h = Math.min(stripHeight, height - y);
    const ny = (y + h / 2) / height * 2 - 1;
    const verticalEdge = Math.abs(ny);
    const rowCurve = verticalEdge ** 1.8;
    const inset = width * rowCurve * 0.022;
    const top = warpY(y);
    const bottom = warpY(y + h);
    const rowHeight = Math.max(1, bottom - top);

    context.drawImage(
      source,
      0,
      y,
      width,
      h,
      inset,
      top,
      width - inset * 2,
      rowHeight,
    );
  }

  const vignette = context.createRadialGradient(width * 0.5, height * 0.46, height * 0.12, width * 0.5, height * 0.5, height * 0.72);
  vignette.addColorStop(0, "rgba(255,255,255,0.05)");
  vignette.addColorStop(0.58, "rgba(255,255,255,0)");
  vignette.addColorStop(0.86, "rgba(0,0,0,0.13)");
  vignette.addColorStop(1, "rgba(0,0,0,0.34)");
  context.fillStyle = vignette;
  context.fillRect(0, 0, width, height);

  context.globalCompositeOperation = "multiply";
  context.fillStyle = "rgba(0,0,0,0.045)";
  for (let y = 0; y < height; y += 5) {
    context.fillRect(0, y, width, 1);
  }
  context.globalCompositeOperation = "source-over";
};

const drawEffect = (
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  activeEffect: EventEffect | undefined,
  effectAgeMs: number,
) => {
  if (!activeEffect || effectAgeMs > 700) {
    return;
  }

  const progress = effectAgeMs / 700;
  const alpha = 1 - progress;
  const success = activeEffect === "grabSuccess";
  const failure = activeEffect === "grabFailure" || activeEffect === "error";
  const color = success ? "194,139,29" : failure ? "190,42,52" : "65,110,180";

  context.save();
  context.globalCompositeOperation = "source-over";
  context.strokeStyle = `rgba(${color}, ${0.55 * alpha})`;
  context.lineWidth = 1.5;
  const size = width * (0.22 + progress * 0.38);
  context.translate(width / 2, height * 0.5);
  context.rotate(Math.PI / 4);
  context.strokeRect(-size / 2, -size / 2, size, size);
  context.restore();

  context.fillStyle = `rgba(${color}, ${0.08 * alpha})`;
  context.fillRect(0, 0, width, height);
};

const wrapTextByWidth = (
  context: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string[] => {
  if (!text) {
    return [""];
  }

  const lines: string[] = [];
  let current = "";
  for (const char of text) {
    const candidate = current + char;
    if (context.measureText(candidate).width <= maxWidth || current.length === 0) {
      current = candidate;
      continue;
    }
    lines.push(current);
    current = char;
  }
  lines.push(current);
  return lines;
};

const drawKeyboardOverlay = (
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  timeMs: number,
  fontFamily: string,
  overlay: KeyboardTextOverlay | undefined,
) => {
  if (!overlay?.visible) {
    return;
  }

  const boxWidth = width * 0.84;
  const boxHeight = height * 0.14;
  const x = (width - boxWidth) / 2;
  const warpSafeBottomMargin = height * 0.09;
  const y = Math.min(height * 0.79, height - boxHeight - warpSafeBottomMargin);
  const padX = boxWidth * 0.028;
  const padY = boxHeight * 0.2;
  const fontSize = Math.max(14, Math.round(height * 0.028));
  const lineHeight = fontSize * 1.32;
  const maxTextWidth = boxWidth - padX * 2;
  const topInset = boxWidth * 0.006;
  const bottomInset = boxWidth * 0.012;
  const topBend = boxHeight * 0.055;
  const bottomBend = boxHeight * 0.1;

  const createPanePath = (inset: number): Path2D => {
    const path = new Path2D();
    const lt = x + topInset + inset;
    const rt = x + boxWidth - topInset - inset;
    const lb = x + bottomInset + inset;
    const rb = x + boxWidth - bottomInset - inset;
    const ty = y + inset;
    const by = y + boxHeight - inset;
    const cx = x + boxWidth * 0.5;

    path.moveTo(lt, ty);
    path.quadraticCurveTo(cx, ty - topBend, rt, ty);
    path.lineTo(rb, by);
    path.quadraticCurveTo(cx, by + bottomBend, lb, by);
    path.closePath();
    return path;
  };

  const outerPane = createPanePath(0);
  const innerPane = createPanePath(2);

  context.save();
  context.fillStyle = "rgba(248, 248, 242, 0.9)";
  context.fill(outerPane);
  context.strokeStyle = "rgba(0, 0, 0, 0.72)";
  context.lineWidth = Math.max(2, width * 0.0026);
  context.stroke(outerPane);
  context.strokeStyle = "rgba(255, 255, 255, 0.52)";
  context.lineWidth = 1;
  context.stroke(innerPane);

  context.globalAlpha = 0.2;
  context.fillStyle = "rgba(0,0,0,0.08)";
  context.save();
  context.clip(innerPane);
  for (let row = y + 1; row < y + boxHeight + bottomBend; row += 3) {
    context.fillRect(x, row, boxWidth, 1);
  }
  context.restore();
  context.globalAlpha = 1;

  context.save();
  context.clip(innerPane);

  context.font = `${fontSize}px ${fontFamily}`;
  context.fillStyle = "rgba(12, 12, 12, 0.95)";
  context.textBaseline = "alphabetic";

  const wrapped = wrapTextByWidth(context, overlay.text, maxTextWidth);
  const maxLines = Math.max(1, Math.floor((boxHeight - padY * 2) / lineHeight));
  const visibleLines = wrapped.slice(Math.max(0, wrapped.length - maxLines));

  visibleLines.forEach((line, index) => {
    const baseline = y + padY + fontSize + index * lineHeight;
    context.fillText(line, x + padX, baseline);
  });

  if (Math.floor(timeMs / 520) % 2 === 0) {
    const cursorLine = visibleLines[visibleLines.length - 1] ?? "";
    const cursorX = x + padX + context.measureText(cursorLine).width + 2;
    const cursorBaseline = y + padY + fontSize + (visibleLines.length - 1) * lineHeight;
    const cursorHeight = Math.max(13, fontSize * 0.9);
    context.fillStyle = "rgba(12, 12, 12, 0.92)";
    context.fillRect(cursorX, cursorBaseline - cursorHeight + 2, Math.max(3, fontSize * 0.13), cursorHeight);
  }

  context.restore();
  context.restore();
};

export const VectorCRTCanvas = ({ mood, keyboardOverlay }: VectorCRTCanvasProps) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [spriteImage, setSpriteImage] = useState<HTMLImageElement | null>(null);
  const [marqueeLogo, setMarqueeLogo] = useState<TrimmedImage | null>(null);
  const [isBlinking, setIsBlinking] = useState(false);
  const machineState = useDisplayStore((s) => s.machineState);
  const moodOverride = useDisplayStore((s) => s.moodOverride);
  const activeEffect = useDisplayStore((s) => s.activeEffect);
  const effectNonce = useDisplayStore((s) => s.effectNonce);
  const visual = visualStateMap[machineState];
  const effectStartedAt = useRef(0);

  const resolvedMood = mood ?? moodOverride ?? (machineState === "attract" ? (isBlinking ? "blink" : "calm") : visual.sprite);
  const spriteSrc = spriteByMood[resolvedMood] ?? spriteByMood[fallbackSpriteMood];

  useEffect(() => {
    if (mood || moodOverride || machineState !== "attract") {
      setIsBlinking(false);
      return;
    }

    let blinkTimer = 0;
    let resetTimer = 0;
    const scheduleBlink = () => {
      blinkTimer = window.setTimeout(() => {
        setIsBlinking(true);
        resetTimer = window.setTimeout(() => {
          setIsBlinking(false);
          scheduleBlink();
        }, ATTRACT_BLINK_DURATION_MS);
      }, ATTRACT_BLINK_INTERVAL_MS);
    };
    scheduleBlink();

    return () => {
      window.clearTimeout(blinkTimer);
      window.clearTimeout(resetTimer);
    };
  }, [machineState, mood, moodOverride]);

  useEffect(() => {
    if (!spriteSrc) {
      setSpriteImage(null);
      return;
    }

    const image = new Image();
    image.decoding = "async";
    image.onload = () => setSpriteImage(image);
    image.src = spriteSrc;
  }, [spriteSrc]);

  useEffect(() => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => {
      const trimCanvas = document.createElement("canvas");
      const trimContext = trimCanvas.getContext("2d", { willReadFrequently: true });
      if (!trimContext) {
        setMarqueeLogo({ image, sx: 0, sy: 0, sw: image.width, sh: image.height });
        return;
      }

      trimCanvas.width = image.width;
      trimCanvas.height = image.height;
      trimContext.drawImage(image, 0, 0);
      const pixels = trimContext.getImageData(0, 0, image.width, image.height).data;
      let minX = image.width;
      let minY = image.height;
      let maxX = 0;
      let maxY = 0;

      for (let y = 0; y < image.height; y += 1) {
        for (let x = 0; x < image.width; x += 1) {
          const alpha = pixels[(y * image.width + x) * 4 + 3];
          if (alpha > 8) {
            minX = Math.min(minX, x);
            minY = Math.min(minY, y);
            maxX = Math.max(maxX, x);
            maxY = Math.max(maxY, y);
          }
        }
      }

      if (minX > maxX || minY > maxY) {
        setMarqueeLogo({ image, sx: 0, sy: 0, sw: image.width, sh: image.height });
        return;
      }

      setMarqueeLogo({
        image,
        sx: minX,
        sy: minY,
        sw: maxX - minX + 1,
        sh: maxY - minY + 1,
      });
    };
    image.src = interrupt26LogoSrc;
  }, []);

  useEffect(() => {
    effectStartedAt.current = performance.now();
  }, [effectNonce]);

  const renderState = useMemo(
    () => ({ visual, activeEffect }),
    [activeEffect, visual],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const visible = canvas.getContext("2d");
    if (!visible) {
      return;
    }

    const source = document.createElement("canvas");
    const sourceContext = source.getContext("2d");
    if (!sourceContext) {
      return;
    }

    let raf = 0;
    let lastWidth = 0;
    let lastHeight = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.6);
      const width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
      const height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
      if (width === lastWidth && height === lastHeight) {
        return;
      }
      lastWidth = width;
      lastHeight = height;
      canvas.width = width;
      canvas.height = height;
      source.width = width;
      source.height = height;
    };

    const drawScene = (timeMs: number) => {
      resize();
      const width = source.width;
      const height = source.height;
      const t = timeMs / 1000;
      const font = `"Courier Prime", "IBM Plex Mono", "Courier New", monospace`;

      sourceContext.clearRect(0, 0, width, height);
      drawBaseBackground(sourceContext, width, height, renderState.visual.backgroundMode);

      const haze = sourceContext.createRadialGradient(width * 0.5, height * 0.45, 0, width * 0.5, height * 0.46, height * 0.7);
      haze.addColorStop(0, "rgba(142, 200, 255, 0.11)");
      haze.addColorStop(0.52, "rgba(247, 247, 242, 0)");
      haze.addColorStop(1, "rgba(247, 247, 242, 0)");
      sourceContext.fillStyle = haze;
      sourceContext.fillRect(0, 0, width, height);

      const contentInsetX = width * 0.045;
      const contentInsetY = height * 0.052;
      sourceContext.save();
      sourceContext.translate(contentInsetX, contentInsetY);
      sourceContext.scale((width - contentInsetX * 2) / width, (height - contentInsetY * 2) / height);

      drawSignalField(
        sourceContext,
        width,
        height,
        t,
        renderState.visual.backgroundMode,
        renderState.visual.intensity,
        renderState.visual.accentColor,
      );

      const frameSize = width * 0.78;
      const frameX = (width - frameSize) / 2;
      const frameY = (height - frameSize) / 2;
      sourceContext.save();
      sourceContext.fillStyle = "rgba(247,247,242,0.18)";
      sourceContext.fillRect(frameX, frameY, frameSize, frameSize);
      sourceContext.restore();

      if (spriteImage) {
        const spriteSize = frameSize * 0.94;
        const spriteX = frameX + (frameSize - spriteSize) / 2;
        const spriteY = frameY + (frameSize - spriteSize) / 2;
        sourceContext.save();
        sourceContext.globalAlpha = 0.18;
        sourceContext.filter = "blur(10px) saturate(1.35)";
        sourceContext.drawImage(spriteImage, spriteX, spriteY, spriteSize, spriteSize);
        sourceContext.restore();
        sourceContext.imageSmoothingEnabled = false;
        sourceContext.drawImage(spriteImage, spriteX, spriteY, spriteSize, spriteSize);
        sourceContext.imageSmoothingEnabled = true;
      }

      sourceContext.restore();
      drawHeaderMarquee(sourceContext, width, height, t, font, marqueeLogo);
      drawKeyboardOverlay(sourceContext, width, height, timeMs, font, keyboardOverlay);
      drawEffect(sourceContext, width, height, renderState.activeEffect, timeMs - effectStartedAt.current);
      drawWarped(visible, source, width, height);

      raf = window.requestAnimationFrame(drawScene);
    };

    raf = window.requestAnimationFrame(drawScene);
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    return () => {
      window.cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [keyboardOverlay, marqueeLogo, renderState, spriteImage]);

  return <canvas ref={canvasRef} className="vector-crt-canvas" aria-label="Agent claw CRT display" />;
};
