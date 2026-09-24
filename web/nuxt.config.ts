export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  modules: ['nuxt-quasar-ui'],
  css: ['~/assets/css/app.scss'],
  app: {
    head: {
      title: 'SignalSlate',
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
        { rel: 'icon', href: '/favicon.ico', sizes: '48x48' },
        { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' },
      ],
    },
  },
  quasar: {
    plugins: ['Notify', 'Dialog', 'Dark', 'LocalStorage'],
    extras: { font: 'roboto-font', fontIcons: ['material-icons'] },
    config: {
      dark: 'auto',
      brand: {
        primary: '#18201F',
        secondary: '#526265',
        accent: '#1B6668',
        positive: '#1B7A4B',
        negative: '#B3261E',
        info: '#4B55B5',
        warning: '#8A5A00',
        dark: '#151D1F',
        'dark-page': '#0E1415',
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
