import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: '../src/wcfi_tools/audio/web', emptyOutDir: true, sourcemap: false },
  server: { port: 1420, strictPort: true },
});
