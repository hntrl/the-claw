import manifestRaw from "../../manifest.json";
import type { SpriteMood } from "../types/display";

type ManifestSpriteEntry = {
  transparent_no_labels: string;
};

type ManifestShape = {
  sprites: Record<string, ManifestSpriteEntry>;
};

const manifest = manifestRaw as ManifestShape;

const resizedSpriteModules = import.meta.glob("../../sprites/transparent_resized/*.png", {
  eager: true,
  import: "default",
}) as Record<string, string>;

const findByFileName = (modules: Record<string, string>, fileName: string): string | undefined => {
  return Object.entries(modules).find(([modulePath]) => modulePath.endsWith(`/${fileName}`))?.[1];
};

export const spriteByMood = Object.fromEntries(
  Object.entries(manifest.sprites).map(([mood, entry]) => {
    const pathParts = entry.transparent_no_labels.split("/");
    const fileName = pathParts[pathParts.length - 1] ?? `${mood}.png`;
    const src = findByFileName(resizedSpriteModules, fileName);

    return [mood, src];
  }),
) as Record<SpriteMood, string | undefined>;

export const fallbackSpriteMood: SpriteMood = "calm";
