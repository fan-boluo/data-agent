import { ref, type Ref } from 'vue'
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
 * pendingApproval 由流处理域在 ApprovalRequired 事件时写入；提交走 POST /api/chat/approval。
 */
export function useApproval(appId: Ref<string | undefined>) {
  const pendingApproval = ref<PendingApproval | null>(null)
  const approvalSubmitting = ref(false)

  // SSE ApprovalRequired 事件 → 弹出审批条
  const showApproval = (eventData: any) => {
    pendingApproval.value = {
      approvalId: String(eventData?.approval_id || ''),
      toolName: String(eventData?.tool_name || '工具调用'),
      level: String(eventData?.level || 'high'),
      reasons: Array.isArray(eventData?.reasons) ? eventData.reasons : [],
      title: String(eventData?.title || ''),
      sql: String(eventData?.sql || ''),
      timeoutSeconds: Number(eventData?.timeout_seconds || 300),
    }
  }

  const clearApproval = () => { pendingApproval.value = null }

  // 提交审批结果：允许/拒绝后后端 future 解除挂起，同一条 SSE 流继续输出
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
      pendingApproval.value = null
    } catch (error: any) {
      message.error('审批提交失败：' + (error.message || '未知错误'))
    } finally {
      approvalSubmitting.value = false
    }
  }

  return { pendingApproval, approvalSubmitting, showApproval, clearApproval, resolveApproval }
}
