# Nekocast Danmaku Frontend

This Vite + Vue 3 project provides the audience view (`/danmaku/:roomId?`) and the operator console (`/danmaku/chat/:roomId?`) for the standalone danmaku service.

## Development

```bash
pnpm install # or npm install / yarn
pnpm dev
```

The dev server proxies `/api/*` to `http://localhost:8000` so you can run the backend locally and test without CORS hassles.

## Build

```bash
pnpm build
```

The production bundle is emitted to `dist/`. Point the backend's static file handler to this folder if you want to serve the UI from the same origin.
