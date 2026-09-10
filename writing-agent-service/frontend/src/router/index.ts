import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/jobs' },
  {
    path: '/hot-news',
    name: 'hot-news',
    component: () => import('@/views/HotNewsView.vue'),
    meta: { title: '热点运营工作台' },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('@/views/JobsView.vue'),
    meta: { title: '写作任务工作台' },
  },
  {
    // Data Loop 有 17 个端点，平铺在一页会失控，按职责拆成六个子面板。
    path: '/data-loop',
    component: () => import('@/views/DataLoopView.vue'),
    redirect: { name: 'data-loop-feedback' },
    meta: { title: 'Data Loop 评审台' },
    children: [
      {
        path: 'feedback',
        name: 'data-loop-feedback',
        component: () => import('@/views/data-loop/FeedbackCasesPanel.vue'),
        meta: { title: '反馈案例' },
      },
      {
        path: 'runs',
        name: 'data-loop-runs',
        component: () => import('@/views/data-loop/RunsPanel.vue'),
        meta: { title: '评测运行' },
      },
      {
        path: 'decisions',
        name: 'data-loop-decisions',
        component: () => import('@/views/data-loop/DecisionsPanel.vue'),
        meta: { title: '运营决策' },
      },
      {
        path: 'datasets',
        name: 'data-loop-datasets',
        component: () => import('@/views/data-loop/DatasetsPanel.vue'),
        meta: { title: '评估数据集' },
      },
      {
        path: 'bundles',
        name: 'data-loop-bundles',
        component: () => import('@/views/data-loop/BundlesPanel.vue'),
        meta: { title: '生产包' },
      },
      {
        path: 'candidates',
        name: 'data-loop-candidates',
        component: () => import('@/views/data-loop/CandidatesPanel.vue'),
        meta: { title: '配置候选' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : 'NewsAgent'
  document.title = `${title} · NewsAgent`
})
