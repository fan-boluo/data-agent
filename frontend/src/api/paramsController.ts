/**
 * 平台资源（tool/skill）与应用授权接口。
 * 后端路由见 backend/src/codegenx/resource_service/router.py
 */
import request from '@/request'

// ---------------------- 平台资源（管理员） ----------------------

export async function listPlatformTools() {
  return request<any>('/api/params/tool/list', { method: 'GET' })
}

export async function updatePlatformTool(body: {
  toolCode: string
  isPlatformEnabled: boolean
  remark?: string | null
}) {
  return request<any>('/api/params/tool/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: body,
  })
}

export async function listPlatformSkills() {
  return request<any>('/api/params/skill/list', { method: 'GET' })
}

export async function updatePlatformSkill(body: {
  skillCode: string
  isPlatformEnabled: boolean
  remark?: string | null
}) {
  return request<any>('/api/params/skill/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: body,
  })
}

// 平台智能体：列表返回实时 LLM 可用性（模型链全熔断即不可用，前端禁止开启）
export async function listPlatformAgents() {
  return request<any>('/api/params/agent/list', { method: 'GET' })
}

export async function updatePlatformAgent(body: {
  agentCode: string
  isEnabled: boolean
  remark?: string | null
}) {
  return request<any>('/api/params/agent/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: body,
  })
}

// ---------------------- 应用授权（owner/admin） ----------------------

export async function listAppGrants(params: { appId: string }) {
  return request<any>('/api/params/app/grant/list', { method: 'GET', params })
}

export async function updateAppGrant(body: API.AppGrantUpdateRequest) {
  return request<any>('/api/params/app/grant/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: body,
  })
}
