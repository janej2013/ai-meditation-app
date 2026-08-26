/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'
import { configDefaults } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  // The companion runner is same-origin in production (CloudFront routes
  // /agent/* to its Function URL); locally, `make dev-agent` serves it on 8080.
  server: {
    proxy: {
      // Longest prefix first: Vite matches keys in order, and '/agent'
      // would otherwise swallow '/agent-lg'. `make dev-agent-lg` serves the
      // LangGraph runner on 8081.
      '/agent-lg': { target: 'http://localhost:8081', changeOrigin: true },
      '/agent': { target: 'http://localhost:8080', changeOrigin: true },
    },
    // The privacy page's test reads docs/privacy.md (?raw) to hold the page
    // to the document; Vite serves nothing outside the project root unless
    // told, so open that one directory and nothing else.
    fs: { allow: ['.', '../docs'] },
  },
  // amazon-cognito-identity-js is written for Node and dereferences `global`,
  // which no browser defines -- the module throws on import, before any of our
  // code runs. esbuild replaces the bare identifier, so this covers the dev
  // prebundle and the production build alike.
  define: {
    global: 'globalThis',
  },
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Drift — guided meditation',
        short_name: 'Drift',
        description: 'Personalised guided meditations from how you feel.',
        // The revised prototype's shell / page surfaces, converted from oklch.
        theme_color: '#0f1625',
        background_color: '#050a17',
        display: 'standalone',
        orientation: 'portrait',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          {
            src: 'icons/icon-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable',
          },
        ],
      },
      workbox: {
        // Precache the app shell (the SPA's own build output).
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        navigateFallback: '/index.html',
        runtimeCaching: [
          {
            // The shared BGM under assets/ is immutable and small: cache it so
            // playback works offline and track switching is instant.
            urlPattern: ({ url }) => url.pathname.startsWith('/assets/bgm/'),
            handler: 'CacheFirst',
            options: {
              cacheName: 'bgm',
              expiration: { maxEntries: 12 },
              // The audio distribution serves CORS responses; cache them as-is.
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          {
            // Signed narration URLs: NetworkOnly. The signature expires in 15
            // minutes and each job's URL is minted per request — caching one
            // would serve dead links and defeat the expiry.
            urlPattern: ({ url }) => url.pathname.startsWith('/jobs/'),
            handler: 'NetworkOnly',
          },
          {
            // API responses: NetworkOnly, no exceptions. Credit balances and
            // job status must be live, and a cached 402/429 would wedge the UI.
            urlPattern: ({ url }) =>
              url.pathname.startsWith('/account') ||
              url.pathname.startsWith('/generate') ||
              url.pathname.startsWith('/billing/'),
            handler: 'NetworkOnly',
          },
          {
            // The companion: a live conversation and a streamed reply. A
            // service worker must never buffer or replay it.
            urlPattern: ({ url }) =>
              url.pathname.startsWith('/agent/') || url.pathname.startsWith('/agent-lg/'),
            handler: 'NetworkOnly',
          },
        ],
      },
    }),
  ],
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    globals: true,
    // e2e/ is Playwright's, not vitest's — the default include pattern would
    // otherwise pick up its *.spec.ts and fail on the foreign test runner.
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
})
