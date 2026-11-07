import request from '@/request'

export async function getMonitorOverview(options?: { [key: string]: any }) {
  return request('/api/stats/admin/monitor/overview', {
    method: 'GET',
    ...(options || {}),
  })
}

export async function getSessionPool(options?: { [key: string]: any }) {
  return request('/api/stats/admin/monitor/pool', {
    method: 'GET',
    ...(options || {}),
  })
}

export async function listAllSessions(
  params?: Record<string, unknown>,
  options?: { [key: string]: any }
) {
  return request('/api/stats/admin/monitor/sessions/all', {
    method: 'GET',
    params: {
      ...(params || {}),
    },
    ...(options || {}),
  })
}

export async function listMonitorSessions(
  params?: Record<string, unknown>,
  options?: { [key: string]: any }
) {
  return request('/api/stats/admin/monitor/sessions', {
    method: 'GET',
    params: {
      ...(params || {}),
    },
    ...(options || {}),
  })
}

export async function getMonitorSessionDetail(sessionId: string, options?: { [key: string]: any }) {
  return request(`/api/stats/admin/monitor/sessions/${sessionId}`, {
    method: 'GET',
    ...(options || {}),
  })
}

export async function getMonitorTurnDetail(
  sessionId: string,
  turnId: string,
  options?: { [key: string]: any }
) {
  return request(`/api/stats/admin/monitor/sessions/${sessionId}/turns/${turnId}`, {
    method: 'GET',
    ...(options || {}),
  })
}

export async function getMonitorTurnSpans(
  sessionId: string,
  turnId: string,
  options?: { [key: string]: any }
) {
  return request(`/api/stats/admin/monitor/sessions/${sessionId}/turns/${turnId}/spans`, {
    method: 'GET',
    ...(options || {}),
  })
}

export async function listMonitorAlerts(
  params?: Record<string, unknown>,
  options?: { [key: string]: any }
) {
  return request('/api/stats/admin/monitor/alerts', {
    method: 'GET',
    params: {
      ...(params || {}),
    },
    ...(options || {}),
  })
}

export async function getMonitorConfig(options?: { [key: string]: any }) {
  return request('/api/stats/admin/monitor/config', {
    method: 'GET',
    ...(options || {}),
  })
}

export async function cleanupMonitorHistory(
  params?: { retentionDays?: number; dryRun?: boolean },
  options?: { [key: string]: any }
) {
  return request('/api/stats/admin/monitor/cleanup', {
    method: 'POST',
    params: {
      ...(params || {}),
    },
    ...(options || {}),
  })
}
