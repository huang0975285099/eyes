import { defineRouter } from '#q-app'
import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import routes from './routes'

export default defineRouter(() => createRouter({
  history: import.meta.env.QCLI_NATIVE_BUILD ? createWebHashHistory() : createWebHistory('/customer/'),
  routes,
  scrollBehavior: () => ({ left: 0, top: 0 }),
}))
