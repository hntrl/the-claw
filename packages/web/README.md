# Web Package

Retro CRT-style vertical TV frontend for the Interrupt claw machine demo.

## Run locally

From repo root:

```bash
pnpm --dir packages/web install
pnpm --dir packages/web dev
```

Or with `just`:

```bash
just web-dev
```

## Optional mock feed

```bash
pnpm --dir packages/web mock:ws
```

Then open:

```txt
http://localhost:5173/?ws=ws://localhost:8787
```

You can also set `VITE_DISPLAY_WS_URL` instead of the query parameter.

## Asset references

- `manifest.json`: mood-to-asset mapping
- `sprites/transparent_no_labels/*.png`: production MVP sprite inputs
- `sprites/quadrants_with_labels/*.png`: labeled review sprites
- `spritesheets/*.png`: original source sheets
- `../../docs/visual_spec.md`: visual/runtime source of truth
