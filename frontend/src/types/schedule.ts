export type ScheduleType = 'once' | 'cron';

export interface ScheduleTask {
  task_id: string;
  room_id: string;
  room_alias: string | null;
  status: string;
  schedule_type: ScheduleType;
  execute_at: string | null;
  next_run_at: string | null;
  cron_expression: string | null;
  cron_description: string | null;
  description: string;
  message: string;
  thread_id: string | null;
  created_by: string | null;
  created_at: string | null;
}

export interface ScheduleListResponse {
  timezone: string;
  tasks: ScheduleTask[];
}

export interface UpdateScheduleRequest {
  room_id: string;
  message?: string;
  description?: string;
  schedule_type?: ScheduleType;
  execute_at?: string;
  cron_expression?: string;
}

export interface CancelScheduleResponse {
  success: boolean;
  message: string;
}
