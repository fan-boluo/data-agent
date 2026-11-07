<template>
  <div id="appManagePage" class="page-shell">
    <a-form layout="inline" :model="searchParams" @finish="doSearch">
      <a-form-item label="项目名称">
        <a-input v-model:value="searchParams.appName" placeholder="输入项目名称" />
      </a-form-item>
      <a-form-item>
        <a-button type="primary" html-type="submit">搜索</a-button>
      </a-form-item>
    </a-form>
    <a-divider />

    <a-table
      :columns="columns"
      :data-source="data"
      :pagination="pagination"
      @change="doTableChange"
      :scroll="{ x: 1200 }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.dataIndex === 'id' || column.dataIndex === 'appName'">
          {{ record[column.dataIndex] }}
        </template>
        <template v-else-if="column.dataIndex === 'createTime'">
          {{ formatTime(record.createTime) }}
        </template>
        <template v-else-if="column.dataIndex === 'owner'">
          {{ record.ownerName || (record.owner ? `用户 ${record.owner}` : '未知用户') }}
        </template>
        <template v-else-if="column.key === 'action'">
          <a-space>
            <a-button type="primary" size="small" @click="editApp(record)">编辑</a-button>
            <a-popconfirm title="确定要删除这个项目吗？" @confirm="deleteApp(record.id)">
              <a-button danger size="small">删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>
    <!-- 编辑弹窗 -->
    <a-modal
      v-model:open="editModalOpen"
      :title="`编辑项目 - ${currentApp?.appName}`"
      width="800px"
      @ok="handleEditOk"
      @cancel="handleEditCancel"
      :footer="null"
    >
      <div class="edit-form-container">
        <a-descriptions :column="2" bordered style="margin-bottom: 24px">
          <a-descriptions-item label="项目ID">{{ currentApp?.id }}</a-descriptions-item>
          <a-descriptions-item label="创建者">
            {{ currentApp?.ownerName || (currentApp?.owner ? `用户 ${currentApp.owner}` : '未知用户') }}
          </a-descriptions-item>
          <a-descriptions-item label="创建时间">{{
            formatTime(currentApp?.createTime)
          }}</a-descriptions-item>
          <a-descriptions-item label="更新时间">{{
            formatTime(currentApp?.updateTime)
          }}</a-descriptions-item>
        </a-descriptions>

        <a-form
          :model="formData"
          :rules="rules"
          layout="vertical"
          @finish="handleSubmit"
          ref="formRef"
        >
          <!-- 项目名称与数据库同行展示 -->
          <a-row :gutter="16">
            <a-col :span="12">
              <a-form-item label="项目名称" name="appName">
                <a-input
                  v-model:value="formData.appName"
                  placeholder="请输入项目名称"
                  :maxlength="50"
                  show-count
                />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="数据库" name="dbName">
                <a-input :value="currentApp?.dbName" placeholder="数据库名称" disabled />
              </a-form-item>
            </a-col>
          </a-row>

          <!-- 项目成员管理：展示普通成员（owner 不在其中），可添加/移除 -->
          <a-form-item label="项目成员">
            <div class="member-add-row">
              <a-select
                v-model:value="selectedUserId"
                show-search
                :options="userOptions"
                :filter-option="filterUserOption"
                :disabled="addingMember"
                placeholder="选择要添加的用户（账号/用户名）"
                style="flex: 1"
              />
              <a-button
                type="primary"
                :disabled="!selectedUserId"
                :loading="addingMember"
                @click="handleAddMember"
              >
                添加
              </a-button>
            </div>
            <a-spin :spinning="memberLoading">
              <div v-if="members.length" class="member-chips">
                <span v-for="m in members" :key="m.userId ?? m.userAccount ?? ''" class="member-chip">
                  {{ m.userName || m.userAccount || m.userId }}
                  <a-popconfirm title="确定移除该成员？" @confirm="handleRemoveMember(m.userId ?? undefined)">
                    <CloseOutlined class="chip-remove" />
                  </a-popconfirm>
                </span>
              </div>
              <div v-else class="member-empty">暂无其他成员，可通过上方下拉框添加</div>
            </a-spin>
          </a-form-item>

          <a-form-item style="margin-bottom: 0">
            <a-space>
              <a-button type="primary" html-type="submit" :loading="submitting">保存修改</a-button>
              <a-button @click="handleEditCancel">关闭</a-button>
            </a-space>
          </a-form-item>
        </a-form>
      </div>
    </a-modal>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { CloseOutlined } from '@ant-design/icons-vue'
import {
  listAppVoByPageByAdmin,
  deleteAppByAdmin,
  updateAppByAdmin,
  getAppVoById,
  listAppMembers,
  addAppMember,
  removeAppMember,
} from '@/api/appController'
import { listUserVoByPage } from '@/api/userController'
import { formatTime } from '@/utils/time'
const router = useRouter()

const columns = [
  { title: 'ID', dataIndex: 'id', width: 80, fixed: 'left' },
  { title: '项目名称', dataIndex: 'appName', width: 150 },
  { title: '项目库', dataIndex: 'dbName', width: 150 },
  { title: '创建者', dataIndex: 'owner', width: 120 },
  { title: '创建时间', dataIndex: 'createTime', width: 160 },
  { title: '操作', key: 'action', width: 130, fixed: 'right' },
]

const data = ref<API.AppVO[]>([])
const total = ref(0)

const searchParams = reactive<API.AppQueryRequest>({
  pageNum: 1,
  pageSize: 10,
})
// 弹窗相关
const editModalOpen = ref(false)
const currentApp = ref<API.AppVO>({})
const formData = reactive({
  appName: '',
})
const formRef = ref()
const submitting = ref(false)

// ---------------------- 项目成员管理 ----------------------
const members = ref<API.AppMemberVO[]>([])
const memberLoading = ref(false)
const users = ref<API.UserVO[]>([])
const selectedUserId = ref<string | undefined>()
const addingMember = ref(false)

// 下拉选项：排除属主与已有成员，label 展示 账号（用户名）
const userOptions = computed(() =>
  users.value
    .filter(
      (u) =>
        String(u.id) !== String(currentApp.value?.owner) &&
        !members.value.some((m) => String(m.userId) === String(u.id)),
    )
    .map((u) => ({
      value: String(u.id),
      label: `${u.userAccount ?? ''}（${u.userName ?? ''}）`,
      keywords: `${u.userAccount ?? ''} ${u.userName ?? ''}`.toLowerCase(),
    })),
)

const filterUserOption = (input: string, option: any) => {
  const keyword = input.trim().toLowerCase()
  if (!keyword) return true
  return String(option?.keywords ?? '').includes(keyword)
}

const fetchMembers = async () => {
  if (!currentApp.value?.id) return
  memberLoading.value = true
  try {
    const res = await listAppMembers({ app_id: String(currentApp.value.id) })
    if (res.data.code === 0 && res.data.data) {
      members.value = res.data.data ?? []
    } else {
      message.error('获取成员列表失败，' + res.data.message)
    }
  } catch (error) {
    console.error('获取成员列表失败：', error)
    message.error('获取成员列表失败')
  } finally {
    memberLoading.value = false
  }
}

const fetchUsers = async () => {
  try {
    const res = await listUserVoByPage({ pageNum: 1, pageSize: 100 })
    if (res.data.code === 0 && res.data.data) {
      users.value = res.data.data.records ?? []
    }
  } catch (error) {
    console.error('获取用户列表失败：', error)
  }
}

const handleAddMember = async () => {
  if (!currentApp.value?.id || !selectedUserId.value) return
  addingMember.value = true
  try {
    const res = await addAppMember({
      appId: String(currentApp.value.id),
      userId: selectedUserId.value,
    })
    if (res.data.code === 0) {
      message.success('已添加成员')
      selectedUserId.value = undefined
      await fetchMembers()
    } else {
      message.error('添加成员失败：' + res.data.message)
    }
  } catch (error) {
    console.error('添加成员失败：', error)
    message.error('添加成员失败')
  } finally {
    addingMember.value = false
  }
}

const handleRemoveMember = async (userId?: string) => {
  if (!currentApp.value?.id || !userId) return
  try {
    const res = await removeAppMember({
      appId: String(currentApp.value.id),
      userId,
    })
    if (res.data.code === 0) {
      message.success('已移除成员')
      await fetchMembers()
    } else {
      message.error('移除成员失败：' + res.data.message)
    }
  } catch (error) {
    console.error('移除成员失败：', error)
    message.error('移除成员失败')
  }
}

const fetchData = async () => {
  try {
    const res = await listAppVoByPageByAdmin({ ...searchParams })
    if (res.data.data) {
      data.value = res.data.data.records ?? []
      total.value = res.data.data.totalRow ?? 0
    } else {
      message.error('获取数据失败，' + res.data.message)
    }
  } catch (error) {
    console.error('获取数据失败：', error)
    message.error('获取数据失败')
  }
}

onMounted(() => {
  fetchData()
})

const pagination = computed(() => ({
  current: searchParams.pageNum ?? 1,
  pageSize: searchParams.pageSize ?? 10,
  total: total.value,
  showSizeChanger: true,
  showTotal: (total: number) => `共 ${total} 条`,
}))

const doTableChange = (page: { current: number; pageSize: number }) => {
  searchParams.pageNum = page.current
  searchParams.pageSize = page.pageSize
  fetchData()
}

const doSearch = () => {
  searchParams.pageNum = 1
  fetchData()
}

const editApp = async (app: API.AppVO) => {
  try {
    const res = await getAppVoById({ id: app.id as string })
    console.log(res)
    if (res.data.code === 0 && res.data.data) {
      currentApp.value = res.data.data
      formData.appName = res.data.data.appName || ''
      editModalOpen.value = true
      // 打开弹框时拉取成员列表与全量用户（下拉框数据源）
      selectedUserId.value = undefined
      fetchMembers()
      fetchUsers()
    } else {
      message.error('获取项目信息失败')
    }
  } catch (error) {
    console.error('获取项目信息失败: ', error)
    message.error('获取项目信息失败')
  }
}

const handleSubmit = async () => {
  if (!currentApp.value?.id) return
  submitting.value = true
  try {
    const res = await updateAppByAdmin({
      id: currentApp.value.id,
      appName: formData.appName,
    })
    if (res.data.code === 0) {
      message.success('修改成功')
      editModalOpen.value = false
      await fetchData()
    } else {
      message.error('修改失败: ' + res.data.message)
    }
  } catch (error) {
    console.error('修改失败: ', error)
    message.error('修改失败')
  } finally {
    submitting.value = false
  }
}

const handleEditOk = async () => {
  await handleSubmit()
}

const handleEditCancel = () => {
  editModalOpen.value = false
  currentApp.value = {}
  formData.appName = ''
  members.value = []
  selectedUserId.value = undefined
}

const rules = {
  appName: [
    { required: true, message: '请输入项目名称', trigger: 'blur' },
    { min: 1, max: 50, message: '项目名称长度在1-50个字符', trigger: 'blur' },
  ],
}

const deleteApp = async (id: string | undefined) => {
  if (!id) return
  try {
    const res = await deleteAppByAdmin({ id })
    if (res.data.code === 0) {
      message.success('删除成功')
      fetchData()
    } else {
      message.error('删除失败：' + res.data.message)
    }
  } catch (error) {
    console.error('删除失败：', error)
    message.error('删除失败')
  }
}
</script>

<style scoped>
/* 页边距统一由全局 .page-shell 模板类提供（styles/theme.css） */

.text-muted {
  color: var(--text-muted);
}
.edit-form-container {
  padding: 16px;
}

.edit-form-container :deep(.ant-descriptions-item-label) {
  background: var(--bg-subtle);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 500;
}

.edit-form-container :deep(.ant-descriptions-item-content) {
  background: transparent;
  color: var(--text-primary);
}

.member-add-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.member-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.member-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: var(--radius-btn, 6px);
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-subtle);
  border: 1px solid var(--border-light);
}

.chip-remove {
  font-size: 10px;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.2s ease;
}

.chip-remove:hover {
  color: var(--error-color, #ff4d4f);
}

.member-empty {
  padding: 8px 0;
  font-size: 13px;
  color: var(--text-muted);
}
</style>
