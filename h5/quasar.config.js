import { defineConfig } from '@quasar/app-vite'

export default defineConfig((ctx) => {
  const nativeBuild = ctx.mode.capacitor || process.env.NATIVE_BUILD === '1'
  return {
  css: ['app.scss'],
  extras: ['material-icons-round'],
  build: {
    target: { browser: ['es2022'], node: 'node22' },
    vueRouterMode: 'history',
    publicPath: nativeBuild ? './' : '/customer/',
    env: { QCLI_NATIVE_BUILD: nativeBuild },
  },
  devServer: { port: 9000, open: false },
  framework: {
    config: {
      brand: { primary: '#22d3b6', secondary: '#4f8cff', dark: '#071a26', positive: '#22d3b6', negative: '#ff6f7d' },
      notify: { position: 'top', timeout: 2200 },
    },
    plugins: ['Notify', 'Dialog', 'Loading'],
  },
  animations: ['fadeIn', 'fadeOut', 'slideInUp'],
  capacitor: { hideSplashscreen: true },
  }
})
