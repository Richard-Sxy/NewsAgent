import { request } from '@/api/http'
import type { PromptInjectionTestRequest, PromptInjectionTestResponse } from '@/api/types'

const BASE = '/api/v1/security-tests'

export const securityTestsApi = {
  promptInjection(body: PromptInjectionTestRequest) {
    return request<PromptInjectionTestResponse>(`${BASE}/prompt-injection`, {
      method: 'POST',
      body,
    })
  },
}
