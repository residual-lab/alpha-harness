import { readFileSync } from 'node:fs'
import path from 'node:path'
import babel from '@rolldown/plugin-babel'
import tailwindcss from '@tailwindcss/vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend runs as a separate process. Proxying /api and /ws in dev means the
// frontend never needs CORS or credentials of its own.
const BACKEND = 'http://127.0.0.1:8000'

// The compiler only rewrites components and hooks, so a .ts file defining no hook would pass
// through Babel unchanged: 47 of 55 today, generated.ts among them.
const HOOK = /(?:function|const) use[A-Z0-9]/
const compiles = (id: string | undefined) => {
  const file = id?.split('?')[0] ?? ''
  if (file.includes('node_modules')) return false
  return /\.tsx$/.test(file) || (/\.ts$/.test(file) && HOOK.test(readFileSync(file, 'utf8')))
}

export default defineConfig({
  // React Compiler through Babel, its reference implementation: the Rust port
  // (`react({ compiler: true })`) builds ten times faster but is still experimental.
  // @babel/core stays on 7: under 8 the compiler skips every component with a default prop.
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()], include: [compiles] }),
    tailwindcss(),
  ],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  // Built into the backend package, which serves it: `uvx alpha-harness` needs no Node.
  build: {
    outDir: path.resolve(import.meta.dirname, '../backend/src/alpha_harness/web'),
    emptyOutDir: true,
    // Screens are already lazy. Splitting the framework out of the entry as well cost 24 kB
    // gzipped more on first load for chunks that cache across wheel upgrades — worth it
    // over a network, worth nothing from 127.0.0.1. The warning is written for a CDN.
    chunkSizeWarningLimit: 700,
  },
  server: {
    // The backend's WebSocket accepts this origin only; a silent move to 5174 would load
    // the app with its live updates refused.
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
})
