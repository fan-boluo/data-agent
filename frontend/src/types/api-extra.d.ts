declare namespace API {
  type AppVO = {
    id?: string
    owner?: string
    ownerName?: string | null
    appName?: string
    dbName?: string
    createTime?: string
    updateTime?: string
  }

  type AppQueryRequest = {
    pageNum?: number
    pageSize?: number
    sortField?: string
    sortOrder?: string
    id?: string
    appName?: string
  }

  type UserSummaryStatsVO = {
    tokenQuota?: number
    usedTokens?: number
    remainingQuota?: number
    totalTokens?: number
    totalRequests?: number
    successRequests?: number
    totalCost?: number
    todayCost?: number
  }

  type UserDailyStatsVO = {
    date?: string
    totalTokens?: number
    totalCost?: number
    requestCount?: number
  }

  type getMyDailyStatsParams = {
    startDate?: string
    endDate?: string
  }

  // ---------------------- 平台资源 / 应用授权 ----------------------
  type PlatformToolVO = {
    toolCode: string
    toolName?: string | null
    description?: string | null
    isPlatformEnabled: boolean
    remark?: string | null
    operatorId?: string | null
    skillCodes?: string[]
    updateTime?: string
  }

  type PlatformSkillVO = {
    skillCode: string
    skillName?: string | null
    description?: string | null
    isPlatformEnabled: boolean
    remark?: string | null
    operatorId?: string | null
    toolCodes?: string[]
    updateTime?: string
  }

  type AppToolGrantVO = {
    toolCode: string
    toolName?: string | null
    description?: string | null
    isPlatformEnabled: boolean
    isEnabled: boolean
    defaultConfig?: Record<string, any>
    overrideConfig?: Record<string, any>
    remark?: string | null
    operatorId?: string | null
    updateTime?: string
  }

  type AppSkillGrantVO = {
    skillCode: string
    skillName?: string | null
    description?: string | null
    isPlatformEnabled: boolean
    isEnabled: boolean
    remark?: string | null
    operatorId?: string | null
    updateTime?: string
  }

  type AppGrantListVO = {
    tools: AppToolGrantVO[]
    skills: AppSkillGrantVO[]
  }

  type AppGrantUpdateRequest = {
    appId: string
    resourceType: 'tool' | 'skill'
    resourceCode: string
    isEnabled: boolean
    overrideConfig?: Record<string, any>
    remark?: string | null
  }

  // 平台智能体：isEnabled 为手动开关；isAvailable 为后端实时计算的 LLM 可用性
  type AgentModelStateVO = {
    model: string
    state: string
    available: boolean
  }

  type PlatformAgentVO = {
    agentCode: string
    agentName?: string | null
    role?: string | null
    description?: string | null
    isEnabled: boolean
    isAvailable: boolean
    unavailableReason?: string | null
    models?: AgentModelStateVO[]
    remark?: string | null
    operatorId?: string | null
    updateTime?: string
  }
}
