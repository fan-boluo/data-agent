<template>
  <div id="appGrantPage" class="page-shell">
    <div class="page-header">
      <div>
        <p class="page-desc">
          在平台已开放的资源范围内，为当前项目授权；平台已下线的资源不可启用。
        </p>
      </div>
      <a-space>
        <a-select
          v-model:value="selectedAppId"
          :options="appOptions"
          show-search
          :filter-option="filterAppOption"
          placeholder="选择项目"
          style="width: 240px"
          :loading="appLoading"
          @change="loadGrants"
        />
        <a-button :loading="loading" :disabled="!selectedAppId" @click="loadGrants">
          刷新
        </a-button>
      </a-space>
    </div>

    <a-empty v-if="!appLoading && !appOptions.length" description="暂无可管理的项目（仅项目 owner 或管理员可授权）" />

    <a-tabs v-else v-model:activeKey="activeTab">
      <a-tab-pane key="tool">
        <template #tab>
          Tool 授权
          <a-tag class="tab-count">{{ enabledToolCount }}/{{ grants.tools?.length || 0 }}</a-tag>
        </template>
        <a-table
          :columns="toolColumns"
          :data-source="grants.tools"
          row-key="toolCode"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 1080 }"
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
            <template v-else-if="column.dataIndex === 'isPlatformEnabled'">
              <a-tag :color="record.isPlatformEnabled ? 'green' : 'red'">
                {{ record.isPlatformEnabled ? '平台开放' : '平台已下线' }}
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'isEnabled'">
              <a-switch
                :checked="record.isEnabled"
                :disabled="!record.isPlatformEnabled"
                checked-children="启用"
                un-checked-children="关闭"
                @change="(checked: any) => requestGrantToggle('tool', record, !!checked)"
              />
            </template>
            <template v-else-if="column.dataIndex === 'overrideConfig'">
              <!-- 覆写配置改为可点击更改：弹窗内展示原工具 JSON 供覆写 -->
              <a-button type="link" size="small" class="override-link" @click="openOverride(record)">
                更改
              </a-button>
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

      <a-tab-pane key="skill">
        <template #tab>
          Skill 授权
          <a-tag class="tab-count">{{ enabledSkillCount }}/{{ grants.skills?.length || 0 }}</a-tag>
        </template>
        <a-table
          :columns="skillColumns"
          :data-source="grants.skills"
          row-key="skillCode"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 1040 }"
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
            <template v-else-if="column.dataIndex === 'isPlatformEnabled'">
              <a-tag :color="record.isPlatformEnabled ? 'green' : 'red'">
                {{ record.isPlatformEnabled ? '平台开放' : '平台已下线' }}
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'isEnabled'">
              <a-switch
                :checked="record.isEnabled"
                :disabled="!record.isPlatformEnabled"
                checked-children="启用"
                un-checked-children="关闭"
                @change="(checked: any) => requestGrantToggle('skill', record, !!checked)"
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

    <!-- 授权开关弹窗：填写备注后提交 -->
    <a-modal
      v-model:open="toggleOpen"
      :title="toggleTitle"
      :confirm-loading="toggleSaving"
      ok-text="确认"
      cancel-text="取消"
      @ok="confirmGrantToggle"
    >
      <a-descriptions :column="1" size="small" bordered class="dialog-meta">
        <a-descriptions-item label="编码">
          <span class="code-text">{{ toggleTarget.code }}</span>
        </a-descriptions-item>
        <a-descriptions-item label="变更后状态">
          <a-tag :color="toggleTarget.nextEnabled ? 'green' : 'red'">
            {{ toggleTarget.nextEnabled ? '启用' : '关闭' }}
          </a-tag>
        </a-descriptions-item>
      </a-descriptions>
      <a-form layout="vertical">
        <a-form-item label="备注">
          <a-textarea
            v-model:value="toggleRemark"
            :rows="2"
            :maxlength="200"
            show-count
            placeholder="记录本次授权的用途或调整原因"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 覆写配置弹窗：默认展示原工具 JSON，可修改后保存 -->
    <a-modal
      v-model:open="overrideOpen"
      :title="`覆写配置 - ${overrideTarget.code}`"
      :confirm-loading="overrideSaving"
      ok-text="保存"
      cancel-text="取消"
      width="640px"
      @ok="saveOverride"
    >
      <a-form layout="vertical">
        <a-form-item label="工具配置（JSON）">
          <a-textarea
            v-model:value="overrideText"
            :rows="12"
            class="config-textarea"
            placeholder='留空或 {} 表示继承平台默认配置，例如 {"timeout": 60}'
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { listAppVoByPageByAdmin, listMyAppVoByPage } from '@/api/appController'
import { listAppGrants, updateAppGrant } from '@/api/paramsController'
import { useLoginUserStore } from '@/stores/loginUser'
import { formatTime } from '@/utils/time'

const loginUserStore = useLoginUserStore()
const isAdmin = computed(() => loginUserStore.loginUser.userRole === 'admin')

const activeTab = ref('tool')
const appLoading = ref(false)
const apps = ref<API.AppVO[]>([])
const selectedAppId = ref<string>()
const loading = ref(false)
const grants = reactive<API.AppGrantListVO>({ tools: [], skills: [] })

const appOptions = computed(() =>
  apps.value.map((app) => ({
    value: String(app.id),
    label: app.appName ? `${app.appName}（${app.id}）` : String(app.id),
    keywords: `${app.appName ?? ''} ${app.id ?? ''}`.toLowerCase(),
  })),
)

const filterAppOption = (input: string, option: any) => {
  const keyword = input.trim().toLowerCase()
  if (!keyword) return true
  return String(option?.keywords ?? '').includes(keyword)
}

const enabledToolCount = computed(() => grants.tools?.filter((item) => item.isEnabled).length ?? 0)
const enabledSkillCount = computed(() => grants.skills?.filter((item) => item.isEnabled).length ?? 0)

const toolColumns = [
  { title: 'Tool 编码', dataIndex: 'toolCode', width: 140 },
  { title: '名称', dataIndex: 'toolName', width: 110 },
  { title: '功能描述', dataIndex: 'description', width: 200, ellipsis: true },
  { title: '平台状态', dataIndex: 'isPlatformEnabled', width: 90 },
  { title: '项目授权', dataIndex: 'isEnabled', width: 90 },
  { title: '覆写配置', dataIndex: 'overrideConfig', width: 70 },
  { title: '备注', dataIndex: 'remark', width: 120, ellipsis: true },
  { title: '操作人', dataIndex: 'operatorId', width: 110, ellipsis: true },
  { title: '更新时间', dataIndex: 'updateTime', width: 150 },
]

const skillColumns = [
  { title: 'Skill 编码', dataIndex: 'skillCode', width: 140 },
  { title: '名称', dataIndex: 'skillName', width: 110 },
  { title: '功能描述', dataIndex: 'description', width: 220, ellipsis: true },
  { title: '平台状态', dataIndex: 'isPlatformEnabled', width: 90 },
  { title: '项目授权', dataIndex: 'isEnabled', width: 90 },
  { title: '备注', dataIndex: 'remark', width: 130, ellipsis: true },
  { title: '操作人', dataIndex: 'operatorId', width: 110, ellipsis: true },
  { title: '更新时间', dataIndex: 'updateTime', width: 150 },
]

const hasOverride = (config?: Record<string, any>) => !!config && Object.keys(config).length > 0

// 仅展示可授权的项目：管理员看全部，普通用户只看自己作为 owner 的项目
const loadApps = async () => {
  appLoading.value = true
  try {
    const res = isAdmin.value
      ? await listAppVoByPageByAdmin({ pageNum: 1, pageSize: 100 })
      : await listMyAppVoByPage({ pageNum: 1, pageSize: 100 })
    if (res.data.code === 0 && res.data.data) {
      const records: API.AppVO[] = res.data.data.records ?? []
      apps.value = isAdmin.value
        ? records
        : records.filter(
            (app) => String(app.owner) === String(loginUserStore.loginUser.id),
          )
      if (!selectedAppId.value && apps.value.length) {
        selectedAppId.value = String(apps.value[0]?.id ?? '')
      }
      if (selectedAppId.value) {
        await loadGrants()
      }
    } else {
      message.error('获取项目列表失败：' + res.data.message)
    }
  } catch (error) {
    console.error('获取项目列表失败：', error)
    message.error('获取项目列表失败')
  } finally {
    appLoading.value = false
  }
}

const loadGrants = async () => {
  if (!selectedAppId.value) return
  loading.value = true
  try {
    const res = await listAppGrants({ appId: selectedAppId.value })
    if (res.data.code === 0 && res.data.data) {
      grants.tools = res.data.data.tools ?? []
      grants.skills = res.data.data.skills ?? []
    } else {
      message.error('获取项目授权失败：' + res.data.message)
    }
  } catch (error) {
    console.error('获取项目授权失败：', error)
    message.error('获取项目授权失败，请确认当前账号是该项目的 owner')
  } finally {
    loading.value = false
  }
}

// ---------------------- 授权开关（先填备注再提交） ----------------------
const toggleOpen = ref(false)
const toggleSaving = ref(false)
const toggleRemark = ref('')
const toggleTarget = reactive({
  type: 'tool' as 'tool' | 'skill',
  code: '',
  nextEnabled: false,
  overrideConfig: {} as Record<string, any>,
})

const toggleTitle = computed(
  () => `${toggleTarget.nextEnabled ? '启用' : '关闭'}${toggleTarget.type === 'tool' ? '工具' : '技能'} - ${toggleTarget.code}`,
)

const requestGrantToggle = (
  type: 'tool' | 'skill',
  record: API.AppToolGrantVO | API.AppSkillGrantVO,
  checked: boolean,
) => {
  toggleTarget.type = type
  toggleTarget.code = type === 'tool' ? (record as API.AppToolGrantVO).toolCode : (record as API.AppSkillGrantVO).skillCode
  toggleTarget.nextEnabled = checked
  // 工具开关不丢覆写配置；技能无覆写配置
  toggleTarget.overrideConfig = type === 'tool' ? ((record as API.AppToolGrantVO).overrideConfig ?? {}) : {}
  toggleRemark.value = record.remark ?? ''
  toggleOpen.value = true
}

const confirmGrantToggle = async () => {
  toggleSaving.value = true
  try {
    const res = await updateAppGrant({
      appId: selectedAppId.value as string,
      resourceType: toggleTarget.type,
      resourceCode: toggleTarget.code,
      isEnabled: toggleTarget.nextEnabled,
      overrideConfig: toggleTarget.type === 'tool' ? toggleTarget.overrideConfig : undefined,
      remark: toggleRemark.value || null,
    })
    if (res.data.code === 0) {
      message.success('操作成功')
      toggleOpen.value = false
      await loadGrants()
    } else {
      message.error('操作失败：' + res.data.message)
    }
  } catch (error) {
    console.error('更新项目授权失败：', error)
    message.error('操作失败')
  } finally {
    toggleSaving.value = false
  }
}

// ---------------------- 覆写配置弹窗（展示原工具 JSON） ----------------------
const overrideOpen = ref(false)
const overrideSaving = ref(false)
const overrideText = ref('{}')
const overrideTarget = reactive({
  code: '',
  isEnabled: false,
  remark: null as string | null,
})

const openOverride = (record: API.AppToolGrantVO) => {
  overrideTarget.code = record.toolCode
  overrideTarget.isEnabled = record.isEnabled
  overrideTarget.remark = record.remark ?? null
  // 已有覆写则展示当前覆写值，否则展示平台原始配置
  const source = hasOverride(record.overrideConfig) ? record.overrideConfig : record.defaultConfig
  overrideText.value = JSON.stringify(source ?? {}, null, 2)
  overrideOpen.value = true
}

const saveOverride = async () => {
  let overrideConfig: Record<string, any> = {}
  if (overrideText.value.trim()) {
    try {
      overrideConfig = JSON.parse(overrideText.value)
    } catch {
      message.error('覆写配置不是合法的 JSON')
      return
    }
  }
  overrideSaving.value = true
  try {
    const res = await updateAppGrant({
      appId: selectedAppId.value as string,
      resourceType: 'tool',
      resourceCode: overrideTarget.code,
      isEnabled: overrideTarget.isEnabled,
      overrideConfig,
      remark: overrideTarget.remark,
    })
    if (res.data.code === 0) {
      message.success('保存成功')
      overrideOpen.value = false
      await loadGrants()
    } else {
      message.error('保存失败：' + res.data.message)
    }
  } catch (error) {
    console.error('保存覆写配置失败：', error)
    message.error('保存失败')
  } finally {
    overrideSaving.value = false
  }
}

onMounted(loadApps)
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  flex-wrap: wrap;
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

.tab-count {
  margin-left: 6px;
  font-size: 12px;
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

.override-link {
  padding: 0;
}

.dialog-meta {
  margin-bottom: 16px;
}

.config-textarea {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
</style>
