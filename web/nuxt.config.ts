export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  modules: ['nuxt-quasar-ui'],
  css: ['~/assets/css/app.scss'],
  quasar: {
    plugins: ['Notify', 'Dialog', 'Dark', 'LocalStorage'],
    extras: { font: 'roboto-font', fontIcons: ['material-icons'] },
    config: {
      dark: 'auto',
      brand: {
        primary: '#3F5EFB',
        secondary: '#5C6470',
        accent: '#3F5EFB',
        positive: '#1F8A5E',
        negative: '#C4392B',
        info: '#3E7CB1',
        warning: '#B8860B',
        dark: '#1B1E23',
        'dark-page': '#121417',
      },
    },
  },
  runtimeConfig: {
    public: {
      // LAN-only — set to the api service's hostname/port in docker-compose.
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
    },
  },
  devServer: { port: 3000 },
})
