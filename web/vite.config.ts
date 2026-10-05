import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          monaco: ['@monaco-editor/react', 'monaco-editor'],
          // Keep markdown rendering out of the main bundle — report tab lazy-loads it.
          markdown: ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
})
