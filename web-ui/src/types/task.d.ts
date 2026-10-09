export interface Task {
  id: number
  task_name: string
  enabled: boolean
  keyword: string
  max_pages: number
  personal_only: boolean
  min_price: string | null
  max_price: string | null
  cron: string | null
  next_run_at?: string | null
  account_state_file?: string | null
  account_strategy: 'auto' | 'fixed' | 'rotate'
  free_shipping?: boolean
  new_publish_option?: string | null
  region?: string | null
  is_running: boolean
}

export type TaskCreate = Omit<Task, 'id' | 'next_run_at' | 'is_running'> & {
  id?: never
  enabled?: boolean
  max_pages?: number
  personal_only?: boolean
  account_strategy?: 'auto' | 'fixed' | 'rotate'
  free_shipping?: boolean
}

export type TaskUpdate = Partial<Omit<Task, 'id' | 'next_run_at'>>
