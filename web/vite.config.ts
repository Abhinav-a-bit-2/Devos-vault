import { defineConfig } from 'vite';
export default defineConfig({
  build: { outDir: '../src/devos/web_dist', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8765', '/openapi.json': 'http://127.0.0.1:8765' } },
});
