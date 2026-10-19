import { createRouter, createWebHistory } from 'vue-router'

// Pages are added by their stories. Everything that exists must be reachable
// without developer tooling; nothing in phase 1 may depend on phase 2 routes.
export const routes = [
  {
    path: '/',
    name: 'workflows',
    component: () => import('@/pages/WorkflowListPage.vue'),
  },
  {
    path: '/workflows/:id',
    name: 'workflow-detail',
    component: () => import('@/pages/WorkflowDetailPage.vue'),
    props: true,
  },
  {
    path: '/workflows/:id/versions',
    name: 'workflow-versions',
    component: () => import('@/pages/WorkflowVersionsPage.vue'),
    props: true,
  },
  {
    path: '/requirements/:id',
    name: 'requirements',
    component: () => import('@/pages/RequirementsPage.vue'),
    props: true,
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('@/pages/ExecutionHistoryPage.vue'),
  },
  {
    path: '/runs/:id',
    name: 'run-progress',
    component: () => import('@/pages/RunProgressPage.vue'),
    props: true,
  },
  {
    path: '/runs/:id/artifacts/:artifactId',
    name: 'artifact',
    component: () => import('@/pages/ArtifactPage.vue'),
    props: true,
  },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('@/pages/SetupPage.vue'),
  },
  {
    path: '/data',
    name: 'data',
    component: () => import('@/pages/DataManagementPage.vue'),
  },
  {
    path: '/maintenance',
    name: 'maintenance',
    component: () => import('@/pages/MaintenancePage.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/pages/NotFoundPage.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
