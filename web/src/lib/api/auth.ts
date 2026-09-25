/**
 * cQuant API — Auth domain (status probe / key verification).
 */

import { api } from './client'

export interface AuthStatus {
  /** Whether the server has CQUANT_API_KEY configured. */
  key_configured: boolean
  /** "strict" | "dev" */
  mode: string
}

export const authApi = {
  status: () => api.get<AuthStatus>('/auth/status'),

  /** 200 = presented key valid; 401 thrown as ApiError otherwise. */
  verify: () => api.get<{ ok: boolean }>('/auth/verify'),
}
