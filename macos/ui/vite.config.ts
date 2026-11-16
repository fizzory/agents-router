// Bundles the three pages into the ignored build/ui-dist, which tauri.conf.json
// embeds as frontendDist. scripts/build_app_ui.py runs it with the pinned Node.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const root = decodeURIComponent(new URL('.', import.meta.url).pathname);

export default defineConfig({
  root,
  base: './',
  publicDir: false,
  cacheDir: `${root}../../build/vite-cache`,
  plugins: [react()],
  build: {
    outDir: `${root}../../build/ui-dist`,
    emptyOutDir: true,
    // macOS 14's WebKit is the floor.
    target: 'safari17',
    cssTarget: 'safari17',
    // Unminified CSS keeps motion tokens such as --tooltip-delay: 400ms exactly
    // as written; the pages read them with parseFloat as milliseconds.
    cssMinify: false,
    // The CSP allows only 'self': no data: URIs and no fetch-based preload polyfill.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: {
        index: `${root}index.html`,
        popover: `${root}popover.html`,
        settings: `${root}settings.html`,
      },
    },
  },
});
