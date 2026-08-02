import { resolve } from 'path'
import { bytecodePlugin, defineConfig, externalizeDepsPlugin } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
    main: {
        plugins: [externalizeDepsPlugin(), bytecodePlugin()]
    },
    preload: {
        input: {
            index: resolve(__dirname, 'src/preload/index.js')
        },
        plugins: [externalizeDepsPlugin(), bytecodePlugin()]
    },
    renderer: {
        publicDir: resolve(__dirname, 'public'),
        resolve: {
            alias: {
                '@renderer': resolve('src/renderer/src'),
                vue: 'vue/dist/vue.esm-bundler.js'
            }
        },
        plugins: [vue()],
        build: {
            rollupOptions: {
                input: {
                    index: resolve(__dirname, 'src/renderer/index.html')
                }
            }
        }
    }
})
