<template>
  <div id="platformResourcePage" class="page-shell">
    <div class="page-header">
      <div>
        <p class="page-desc">
          平台级总开关，关闭后全平台所有项目均不可用
        </p>
      </div>
      <a-button :loading="loading" @click="loadAll">刷新</a-button>
    </div>

    <a-tabs v-model:activeKey="activeTab">
      <a-tab-pane key="tool" tab="Tool 管理">
        <a-table
          :columns="toolColumns"
          :data-source="tools"
          row-key="toolCode"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 1060 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'toolCode'">
              <span class="code-text">{{ record.toolCode }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'description'">
              <span class="clamp-text" :title="record.description || ''">
                {{ record.description || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'skillCodes'">
              <a-space v-if="record.skillCodes?.length" wrap :size="4">
                <a-tag v-for="code in record.skillCodes" :key="code">{{ code }}</a-tag>
              </a-space>
              <span v-else class="empty-text">无关联</span>
            </template>
            <template v-else-if="column.dataIndex === 'isPlatformEnabled'">
              <a-switch
                :checked="record.isPlatformEnabled"
                checked-children="开放"
                un-checked-children="关闭"
                @change="(checked: any) => requestToggle('tool', record, !!checked)"
              />
            </template>
            <template v-else-if="column.dataIndex === 'remark'">
              <span class="clamp-text" :title="record.remark || ''">{{ record.remark || '—' }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'operatorId'">
              <span class="clamp-text" :title="record.operatorId || ''">
                {{ record.operatorId || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'updateTime'">
              {{ formatTime(record.updateTime) || '—' }}
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="skill" tab="Skill 管理">
        <a-table
          :columns="skillColumns"
          :data-source="skills"
          row-key="skillCode"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 1050 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'skillCode'">
              <span class="code-text">{{ record.skillCode }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'description'">
              <span class="clamp-text" :title="record.description || ''">
                {{ record.description || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'toolCodes'">
              <!-- 包含的 Tool 暂不展示，占位留空 -->
              <span></span>
            </template>
            <template v-else-if="column.dataIndex === 'isPlatformEnabled'">
              <a-switch
                :checked="record.isPlatformEnabled"
                checked-children="开放"
                un-checked-children="关闭"
                @change="(checked: any) => requestToggle('skill', record, !!checked)"
              />
            </template>
            <template v-else-if="column.dataIndex === 'remark'">
              <span class="clamp-text" :title="record.remark || ''">{{ record.remark || '—' }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'operatorId'">
              <span class="clamp-text" :title="record.operatorId || ''">
                {{ record.operatorId || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'updateTime'">
              {{ formatTime(record.updateTime) || '—' }}
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="agent" tab="Agent 管理">
        <a-alert
          type="info"
          show-icon
          class="agent-tip"
          message="可用性为实时检测：对应模型链全部熔断打开时智能体自动下线，冷却恢复后自动可用；不可用的智能体不能手动开启。"
        />
        <a-table
          :columns="agentColumns"
          :data-source="agents"
          row-key="agentCode"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 1050 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'agentCode'">
              <span class="code-text">{{ record.agentCode }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'description'">
              <span class="clamp-text" :title="record.description || ''">
                {{ record.description || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'isAvailable'">
              <a-tooltip>
                <template #title>
                  <div v-for="m in record.models || []" :key="m.model">
                    {{ m.model }}：{{ m.state }}{{ m.available ? '' : '（熔断打开）' }}
                  </div>
                  <div v-if="record.unavailableReason">{{ record.unavailableReason }}</div>
                </template>
                <a-tag :color="record.isAvailable ? 'green' : 'red'">
                  {{ record.isAvailable ? '可用' : '不可用' }}
                </a-tag>
              </a-tooltip>
              <span v-if="!record.isAvailable" class="empty-text">已自动下线</span>
            </template>
            <template v-else-if="column.dataIndex === 'isEnabled'">
              <a-switch
                :checked="record.isEnabled"
                :disabled="!record.isAvailable && !record.isEnabled"
                checked-children="启用"
                un-checked-children="停用"
                @change="(checked: any) => requestToggle('agent', record, !!checked)"
              />
            </template>
            <template v-else-if="column.dataIndex === 'remark'">
              <span class="clamp-text" :title="record.remark || ''">{{ record.remark || '—' }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'operatorId'">
              <span class="clamp-text" :title="record.operatorId || ''">
                {{ record.operatorId || '—' }}
              </span>
            </template>
            <template v-else-if="column.dataIndex === 'updateTime'">
              {{ formatTime(record.updateTime) || '—' }}
            </template>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>

    <!-- 平台状态切换弹窗：填写备注后提交（操作人由后端按登录态记录） -->
    <a-modal
      v-model:open="toggleOpen"
      :title="toggleTitle"
      :confirm-loading="toggleSaving"
      ok-text="确认"
      cancel-text="取消"
      @ok="confirmToggle"
    >
      <a-descriptions :column="1" size="small" bordered class="toggle-meta">
        <a-descriptions-item label="编码">
          <span class="code-text">{{ toggleTarget.code }}</span>
        </a-descriptions-item>
        <a-descriptions-item label="变更后状态">
          <a-tag :color="toggleTarget.nextEnabled ? 'green' : 'red'">
            {{ toggleTarget.nextEnabled ? '开放' : '关闭' }}
          </a-tag>
        </a-descriptions-item>
      </a-descriptions>

      <a-form layout="vertical" class="toggle-form">
        <a-form-item label="备注">
          <a-textarea
            v-model:value="toggleRemark"
            :rows="3"
            :maxlength="500"
            show-count
            placeholder="记录下线原因、风险说明或维护信息"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  listPlatformAgents,
  listPlatformSkills,
  listPlatformTools,
  updatePlatformAgent,
  updatePlatformSkill,
  updatePlatformTool,
} from '@/api/paramsController'
import { formatTime } from '@/utils/time'

const activeTab = ref('tool')
const loading = ref(false)
const tools = ref<API.PlatformToolVO[]>([])
const skills = ref<API.PlatformSkillVO[]>([])
const agents = ref<API.PlatformAgentVO[]>([])

const toolColumns = [
  { title: 'Tool 编码', dataIndex: 'toolCode', width: 140 },
  { title: '名称', dataIndex: 'toolName', width: 110 },
  { title: '功能描述', dataIndex: 'description', width: 200, ellipsis: true },
  { title: '包含它的 Skill', dataIndex: 'skillCodes', width: 140 },
  { title: '平台状态', dataIndex: 'isPlatformEnabled', width: 90 },
  { title: '备注', dataIndex: 'remark', width: 120, ellipsis: true },
  { title: '操作人', dataIndex: 'operatorId', width: 110, ellipsis: true },
  { title: '更新时间', dataIndex: 'updateTime', width: 150 },
]

const skillColumns = [
  { title: 'Skill 编码', dataIndex: 'skillCode', width: 140 },
  { title: '名称', dataIndex: 'skillName', width: 110 },
  { title: '功能描述', dataIndex: 'description', width: 210, ellipsis: true },
  { title: '包含的 Tool', dataIndex: 'toolCodes', width: 120 },
  { title: '平台状态', dataIndex: 'isPlatformEnabled', width: 90 },
  { title: '备注', dataIndex: 'remark', width: 120, ellipsis: true },
  { title: '操作人', dataIndex: 'operatorId', width: 110, ellipsis: true },
  { title: '更新时间', dataIndex: 'updateTime', width: 150 },
]

const agentColumns = [
  { title: 'Agent 编码', dataIndex: 'agentCode', width: 130 },
  { title: '名称', dataIndex: 'agentName', width: 130 },
  { title: '职责描述', dataIndex: 'description', width: 210, ellipsis: true },
  { title: 'LLM 可用性', dataIndex: 'isAvailable', width: 130 },
  { title: '全局启停', dataIndex: 'isEnabled', width: 90 },
  { title: '备注', dataIndex: 'remark', width: 110, ellipsis: true },
  { title: '操作人', dataIndex: 'operatorId', width: 110, ellipsis: true },
  { title: '更新时间', dataIndex: 'updateTime', width: 150 },
]

const loadAll = async () => {
  loading.value = true
  try {
    const [toolRes, skillRes, agentRes] = await Promise.all([
      listPlatformTools(),
      listPlatformSkills(),
      listPlatformAgents(), // 每次进入/刷新后端实时重算模型链熔断可用性
    ])
    if (toolRes.data.code === 0) {
      tools.value = toolRes.data.data ?? []
    } else {
      message.error('获取工具列表失败：' + toolRes.data.message)
    }
    if (skillRes.data.code === 0) {
      skills.value = skillRes.data.data ?? []
    } else {
      message.error('获取技能列表失败：' + skillRes.data.message)
    }
    if (agentRes.data.code === 0) {
      agents.value = agentRes.data.data ?? []
    } else {
      message.error('获取智能体列表失败：' + agentRes.data.message)
    }
  } catch (error) {
    console.error('获取平台资源失败：', error)
    message.error('获取平台资源失败，请确认 agent schema 已初始化')
  } finally {
    loading.value = false
  }
}

// ---------------------- 平台状态切换（先填备注再提交） ----------------------
const toggleOpen = ref(false)
const toggleSaving = ref(false)
const toggleRemark = ref('')
const toggleTarget = reactive({
  type: 'tool' as 'tool' | 'skill' | 'agent',
  code: '',
  nextEnabled: true,
})

const toggleTitle = computed(() => {
  const label = toggleTarget.type === 'tool' ? '工具' : toggleTarget.type === 'skill' ? '技能' : '智能体'
  return `${toggleTarget.nextEnabled ? '开放' : '关闭'}${label} - ${toggleTarget.code}`
})

const requestToggle = (
  type: 'tool' | 'skill' | 'agent',
  record: API.PlatformToolVO | API.PlatformSkillVO | API.PlatformAgentVO,
  checked: boolean,
) => {
  const code =
    type === 'tool'
      ? (record as API.PlatformToolVO).toolCode
      : type === 'skill'
        ? (record as API.PlatformSkillVO).skillCode
        : (record as API.PlatformAgentVO).agentCode
  // Agent 开启前做可用性拦截：模型链全部熔断时不允许启用
  if (type === 'agent' && checked && !(record as API.PlatformAgentVO).isAvailable) {
    message.warning(`智能体 ${code} 模型链当前不可用，无法开启`)
    return
  }
  toggleTarget.type = type
  toggleTarget.code = code
  toggleTarget.nextEnabled = checked
  toggleRemark.value = record.remark ?? '' // 预填原备注，便于修改
  toggleOpen.value = true
}

const confirmToggle = async () => {
  toggleSaving.value = true
  try {
    const remark = toggleRemark.value || null
    const res =
      toggleTarget.type === 'tool'
        ? await updatePlatformTool({
            toolCode: toggleTarget.code,
            isPlatformEnabled: toggleTarget.nextEnabled,
            remark,
          })
        : toggleTarget.type === 'skill'
          ? await updatePlatformSkill({
              skillCode: toggleTarget.code,
              isPlatformEnabled: toggleTarget.nextEnabled,
              remark,
            })
          : await updatePlatformAgent({
              agentCode: toggleTarget.code,
              isEnabled: toggleTarget.nextEnabled,
              remark,
            })
    if (res.data.code === 0) {
      message.success('操作成功')
      toggleOpen.value = false
      await loadAll() // 重新拉取，操作人/更新时间以服务端为准
    } else {
      message.error('操作失败：' + res.data.message)
    }
  } catch (error) {
    console.error('更新平台资源失败：', error)
    message.error('操作失败')
  } finally {
    toggleSaving.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
}

.page-desc {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}

.code-text {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 13px;
  color: var(--text-primary);
}

.clamp-text {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

.empty-text {
  font-size: 13px;
  color: var(--text-muted);
}

.agent-tip {
  margin-bottom: 12px;
}

.toggle-meta {
  margin-bottom: 16px;
}
</style>
