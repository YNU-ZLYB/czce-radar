import { createRouter, createWebHistory } from 'vue-router'

import HomeView from '../views/HomeView.vue'

const DigestView = () => import('../views/DigestView.vue')
const ScorecardView = () => import('../views/ScorecardView.vue')

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView, meta: { title: '卡片' } },
    { path: '/digest', name: 'digest', component: DigestView, meta: { title: '简报' } },
    { path: '/scorecard', name: 'scorecard', component: ScorecardView, meta: { title: '评分' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to) => {
  document.title = `CZCE Radar · ${String(to.meta.title ?? '郑商所研判')}`
})
