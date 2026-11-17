<template>
  <div id="platformConfigPage" class="page-shell">
    <div class="page-header">
      <div>
        <p class="page-desc">平台级运行参数与操作拦截配置</p>
      </div>
    </div>

    <a-tabs v-model:activeKey="activeTab">
      <a-tab-pane key="runtime" tab="运行参数">
        <a-empty description="暂未配置运行参数" />
      </a-tab-pane>
      <a-tab-pane key="intercept" tab="操作拦截">
        <a-empty description="暂未配置操作拦截规则" />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

// 当前激活的配置页签：运行参数 / 操作拦截
const activeTab = ref('runtime')
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.page-desc {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}
</style>
