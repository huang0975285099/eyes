// electron.vite.config.mjs
import { resolve } from "path";
import { bytecodePlugin, defineConfig, externalizeDepsPlugin } from "electron-vite";
import vue from "@vitejs/plugin-vue";
var __electron_vite_injected_dirname = "D:\\project\\eyes\\app";
var electron_vite_config_default = defineConfig({
  main: {
    plugins: [externalizeDepsPlugin(), bytecodePlugin()]
  },
  preload: {
    input: {
      index: resolve(__electron_vite_injected_dirname, "src/preload/index.js")
    },
    plugins: [externalizeDepsPlugin(), bytecodePlugin()]
  },
  renderer: {
    publicDir: resolve(__electron_vite_injected_dirname, "public"),
    resolve: {
      alias: {
        "@renderer": resolve("src/renderer/src"),
        vue: "vue/dist/vue.esm-bundler.js"
      }
    },
    plugins: [vue()],
    build: {
      rollupOptions: {
        input: {
          index: resolve(__electron_vite_injected_dirname, "src/renderer/index.html")
        }
      }
    }
  }
});
export {
  electron_vite_config_default as default
};
