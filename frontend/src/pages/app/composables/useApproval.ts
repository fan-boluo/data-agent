import { computed, ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import { getAuthHeaders } from './chatHelpers'

export interface PendingApproval {
  approvalId: string
  toolName: string
  level: string
  reasons: string[]
  title: string
  sql: string
  timeoutSeconds: number
}

/**
 * HITL 审批弹窗状态（高风险工具执行前挂起，等待人工确认）。
 * 并行工具调用会同时产生多条审批：用队列保存，队首为当前展示项，逐条处理，
 * 避免单值覆盖导致看不见的审批最终超时自动拒绝。提交走 POST /api/chat/approval。
 */
export function useApproval(appId: Ref<string | undefined>) {
  const pendingQueue = ref<PendingApproval[]>([])
  // 队首即当前展示的审批条
  const pendingApproval = computed<PendingApproval | null>(() => pendingQueue.value[0] ?? null)
  // 待审批总数（>1 时面板提示逐条处理）
  const pendingCount = computed(() => pendingQueue.value.length)
  const approvalSubmitting = ref(false)

  // SSE ApprovalRequired 事件 → 入队（同一 approval_id 重放不重复入队）
  const showApproval = (eventData: any) => {
    const item: PendingApproval = {
      approvalId: String(eventData?.approval_id || ''),
      toolName: String(eventData?.tool_name || '工具调用'),
      level: String(eventData?.level || 'high'),
      reasons: Array.isArray(eventData?.reasons) ? eventData.reasons : [],
      title: String(eventData?.title || ''),
      sql: String(eventData?.sql || ''),
      timeoutSeconds: Number(eventData?.timeout_seconds || 1800),
    }
    if (!pendingQueue.value.some(p => p.approvalId === item.approvalId)) {
      pendingQueue.value.push(item)
    }
  }

  const clearApproval = () => { pendingQueue.value = [] }

  // 提交审批结果：允许/拒绝后后端 future 解除挂起，同一条 SSE 流继续输出；队首出队后自动展示下一条
  const resolveApproval = async (approved: boolean) => {
    const item = pendingApproval.value
    if (!item || approvalSubmitting.value) return
    approvalSubmitting.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await fetch(`${baseURL}/api/chat/approval`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({
          appId: String(appId.value),
          approvalId: item.approvalId,
          approved,
          reason: approved ? '用户允许执行' : '用户拒绝执行',
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      message.success(approved ? '已允许执行' : '已拒绝执行')
      pendingQueue.value.shift()
    } catch (error: any) {
      message.error('审批提交失败：' + (error.message || '未知错误'))
    } finally {
      approvalSubmitting.value = false
    }
  }

  return { pendingApproval, pendingCount, approvalSubmitting, showApproval, clearApproval, resolveApproval }
}
