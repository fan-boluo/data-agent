import { computed, ref } from 'vue'

export interface TaskInfo {
  id: number
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'deleted'
  blockedBy: number[]
  blocks: number[]
  owner: string
}

/**
 * 计划任务看板状态（task_create/task_update 与 ToolExecutionEnd.task_data 推进）。
 * upsertTaskNode 由时间线域注入，保持看板任务与时间线任务节点同步。
 */
export function useTaskBoard(upsertTaskNode: (task: { id?: number; subject?: string; status?: string }) => void) {
  const tasks = ref<TaskInfo[]>([])
  const visibleTasks = computed(() => tasks.value.filter(t => t.status !== 'deleted'))

  // ToolExecutionEnd.task_data：看板任务增改 + 时间线任务节点同步
  const applyTaskData = (td: any) => {
    if (td.action === 'create') {
      tasks.value.push(td.task as TaskInfo)
    } else if (td.action === 'update') {
      const existingIdx = tasks.value.findIndex(t => t.id === td.task.id)
      if (existingIdx >= 0) {
        tasks.value[existingIdx] = { ...tasks.value[existingIdx], ...td.task }
      }
    }
    // 时间线：计划任务节点创建/更新（含进行中锚点切换）
    if (td.task?.id) upsertTaskNode(td.task)
  }

  const resetTasks = () => { tasks.value = [] }

  return { tasks, visibleTasks, applyTaskData, resetTasks }
}
