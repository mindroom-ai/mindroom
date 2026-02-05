import { SkillDetail, SkillSummary } from '@/types/skills';

const API_BASE = '/api';

async function requestJSON<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);

  if (!response.ok) {
    const message = await getErrorMessage(response, 'Request failed');
    throw new Error(message);
  }

  return (await response.json()) as T;
}

async function getErrorMessage(response: Response, fallback: string) {
  try {
    const payload = await response.json();
    if (payload && typeof payload.detail === 'string') {
      return payload.detail;
    }
  } catch {
    // Ignore JSON parse failures and fall back to default.
  }
  return `${fallback} (Error ${response.status})`;
}

export async function listSkills(): Promise<SkillSummary[]> {
  return requestJSON<SkillSummary[]>(`${API_BASE}/skills`);
}

export async function getSkill(skillName: string): Promise<SkillDetail> {
  return requestJSON<SkillDetail>(`${API_BASE}/skills/${encodeURIComponent(skillName)}`);
}

export async function updateSkill(skillName: string, content: string): Promise<void> {
  await requestJSON<{ success: boolean }>(`${API_BASE}/skills/${encodeURIComponent(skillName)}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ content }),
  });
}
