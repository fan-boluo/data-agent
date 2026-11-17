import request from '@/request'

// 评测题集列表（题数 / 已校验数）
export async function listEvalDatasets(options?: { [key: string]: any }) {
  return request('/api/admin/eval/datasets', {
    method: 'GET',
    ...(options || {}),
  })
}

// 历史评测运行列表（按创建时间倒序）
export async function listEvalRuns(
  params?: { datasetId?: number; limit?: number },
  options?: { [key: string]: any }
) {
  return request('/api/admin/eval/runs', {
    method: 'GET',
    params: {
      ...(params || {}),
    },
    ...(options || {}),
  })
}
