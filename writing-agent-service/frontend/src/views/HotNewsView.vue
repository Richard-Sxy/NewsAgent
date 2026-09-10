<script setup lang="ts">
import { RouterLink } from 'vue-router'

import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

/**
 * 热点榜的确定性计算已在后端 `app/analytics` 落地，但**没有对外读接口**。
 * 企业版不保留前端假数据：运营看到的排名必须可追溯到后端计算，
 * 否则榜单无法审计。这里只声明前端期望的契约。
 */
const expectedContract = [
  {
    method: 'GET',
    path: '/api/v1/hot-news/runs',
    purpose: '当前窗口的榜单与热度分量拆解',
  },
  {
    method: 'GET',
    path: '/api/v1/hot-news/runs/{run_id}',
    purpose: '单条热点的 12 点趋势与决策记录',
  },
  {
    method: 'POST',
    path: '/api/v1/hot-news/decisions',
    purpose: '运营决策（采纳 / 拒绝 / 修正）',
  },
]
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>热点运营工作台</h2>
      <span class="na-badge na-badge--sample">后端接口未接入</span>
    </div>

    <div class="na-notice">
      该模块在旧版单文件控制台中渲染的是前端内置示例数据。企业版不保留假数据，
      因此这里不展示任何排名或趋势 —— 避免运营依据伪造的榜单做决策。
    </div>

    <h3>当前可用能力</h3>
    <p class="na-muted">
      热点的判决链路已经存在，可通过
      <RouterLink :to="{ name: 'data-loop-feedback' }">Data Loop 评审台</RouterLink>
      记录运营决策与反馈案例，那组的写接口是真实的，可以直接使用。
    </p>

    <p v-if="!appStore.hotNewsEnabled" class="na-muted">
      运行时配置中 <code>enableHotNews</code> 为 false，本模块在网关侧未开放。
    </p>
  </section>

  <section class="na-card">
    <div class="na-card__head">
      <h2>接入前提：后端需要补齐的只读接口</h2>
    </div>
    <table class="na-table">
      <thead>
        <tr>
          <th style="width: 76px">方法</th>
          <th style="width: 300px">路径</th>
          <th>用途</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in expectedContract" :key="item.path">
          <td>
            <span class="na-badge">{{ item.method }}</span>
          </td>
          <td class="na-mono">{{ item.path }}</td>
          <td class="na-muted">{{ item.purpose }}</td>
        </tr>
      </tbody>
    </table>
    <p class="na-muted">
      接口就绪后，本页只需替换数据来源，页面结构与交互无需重写。
    </p>
  </section>
</template>
