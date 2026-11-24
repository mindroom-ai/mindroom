import { fetchJSON } from "@/lib/api";
import { SkillDetail, SkillSummary } from "@/types/skills";

export async function listSkills(): Promise<SkillSummary[]> {
  return fetchJSON<SkillSummary[]>("/api/skills");
}

export async function getSkill(skillName: string): Promise<SkillDetail> {
  return fetchJSON<SkillDetail>(`/api/skills/${encodeURIComponent(skillName)}`);
}

export async function updateSkill(
  skillName: string,
  content: string,
): Promise<void> {
  await fetchJSON<void>(`/api/skills/${encodeURIComponent(skillName)}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

export async function createSkill(
  name: string,
  description: string,
): Promise<SkillSummary> {
  return fetchJSON<SkillSummary>("/api/skills", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
}

export async function deleteSkill(skillName: string): Promise<void> {
  await fetchJSON<void>(`/api/skills/${encodeURIComponent(skillName)}`, {
    method: "DELETE",
  });
}
