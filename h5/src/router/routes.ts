import type { RouteRecordRaw } from 'vue-router'
import CustomerPortal from '../pages/CustomerPortal.vue'

const routes: RouteRecordRaw[] = [
  { path: '/', component: CustomerPortal },
  { path: '/:catchAll(.*)*', redirect: '/' },
]

export default routes
