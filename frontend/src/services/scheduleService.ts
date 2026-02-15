import type {
  CancelScheduleResponse,
  ScheduleListResponse,
  ScheduleTask,
  UpdateScheduleRequest,
} from '@/types/schedule';
import { getAuthHeaders } from '@/lib/api';

const API_BASE = '/api/schedules';

export async function listSchedules(roomId?: string): Promise<ScheduleListResponse> {
  const url = new URL(API_BASE, window.location.origin);
  if (roomId) {
    url.searchParams.set('room_id', roomId);
  }

  const response = await fetch(url.pathname + url.search, {
    method: 'GET',
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to load schedules (Error ${response.status})`);
  }

  return response.json();
}

export async function updateSchedule(
  taskId: string,
  payload: UpdateScheduleRequest
): Promise<ScheduleTask> {
  const response = await fetch(`${API_BASE}/${encodeURIComponent(taskId)}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      ...getAuthHeaders(),
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const errorMessage = await extractErrorMessage(response);
    throw new Error(errorMessage);
  }

  return response.json();
}

export async function cancelSchedule(
  taskId: string,
  roomId: string
): Promise<CancelScheduleResponse> {
  const url = new URL(`${API_BASE}/${encodeURIComponent(taskId)}`, window.location.origin);
  url.searchParams.set('room_id', roomId);

  const response = await fetch(url.pathname + url.search, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const errorMessage = await extractErrorMessage(response);
    throw new Error(errorMessage);
  }

  return response.json();
}

async function extractErrorMessage(response: Response): Promise<string> {
  try {
    const data = await response.json();
    if (typeof data?.detail === 'string') {
      return data.detail;
    }
  } catch {
    // Fall through to default message
  }
  return `Request failed (Error ${response.status})`;
}
