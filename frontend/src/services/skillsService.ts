import { fetchAPI, API_BASE_URL } from '@/lib/api';
import { SkillDetail, SkillSummary } from '@/types/skills';

export async function listSkills(): Promise<SkillSummary[]> {
  return fetchAPI(`${API_BASE_URL}/api/skills`) as Promise<SkillSummary[]>;
}

export async function getSkill(skillName: string): Promise<SkillDetail> {
  return fetchAPI(
    `${API_BASE_URL}/api/skills/${encodeURIComponent(skillName)}`
  ) as Promise<SkillDetail>;
}

export async function updateSkill(skillName: string, content: string): Promise<void> {
  await fetchAPI(`${API_BASE_URL}/api/skills/${encodeURIComponent(skillName)}`, {
    method: 'PUT',
    body: JSON.stringify({ content }),
  });
}

export async function createSkill(name: string, description: string): Promise<SkillSummary> {
  return fetchAPI(`${API_BASE_URL}/api/skills`, {
    method: 'POST',
    body: JSON.stringify({ name, description }),
  }) as Promise<SkillSummary>;
}

export async function deleteSkill(skillName: string): Promise<void> {
  await fetchAPI(`${API_BASE_URL}/api/skills/${encodeURIComponent(skillName)}`, {
    method: 'DELETE',
  });
}
