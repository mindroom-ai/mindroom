import { useMemo } from 'react';
import { listSkills } from '@/services/skillsService';
import { SkillSummary } from '@/types/skills';
import { useFetchData } from './useFetchData';

const DEFAULT: SkillSummary[] = [];

export function useSkills() {
  const fetcher = useMemo(() => () => listSkills(), []);
  const { data: skills, ...rest } = useFetchData(fetcher, DEFAULT);
  return { skills, ...rest };
}
