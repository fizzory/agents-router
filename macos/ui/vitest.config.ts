// Component tests for the React pages (src/**/*.test.tsx) in jsdom. The pure
// logic keeps its own node --test suite in logic/. scripts/build_app_ui.py --test runs this.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const root = decodeURIComponent(new URL('.', import.meta.url).pathname);

export default defineConfig({
  root,
  cacheDir: `${root}../../build/vite-cache`,
  plugins: [react()],
  server: { fs: { allow: [`${root}../..`] } },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
    restoreMocks: true,
    // Stylesheets read as text (?raw): motion.css for its tokens, and the control
    // sheets for the hit-target sizes. Keep them out of Vitest's CSS stubbing.
    css: { include: [/(?:motion|tokens|shared|controls|settings|popover)\.css/] },
  },
});
