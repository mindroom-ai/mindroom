export type SkillOrigin = 'bundled' | 'plugin' | 'user' | 'custom';

export interface SkillSummary {
  name: string;
  description: string;
  path: string;
  origin: SkillOrigin;
  can_edit: boolean;
}

export interface SkillDetail extends SkillSummary {
  content: string;
}
