import { defineCapacitorConfig } from '@quasar/app-vite/capacitor'

export default defineCapacitorConfig({
  appId: 'cn.allseeingeyes.customer',
  appName: '千里眼客户平台',
  plugins: { SplashScreen: { launchAutoHide: true, backgroundColor: '#061722' } },
  android: { allowMixedContent: true },
})
