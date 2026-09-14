export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  modules: ['nuxt-quasar-ui'],
  quasar: {
    plugins: ['Notify'],
    extras: { font: 'roboto-font', icons: ['material-icons'] },
  },
  runtimeConfig: {
    public: {
      // LAN-only — set to the api service's hostname/port in docker-compose.
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
    },
  },
  devServer: { port: 3000 },
})
