import { useState, useEffect } from 'react';
import { listSkills } from '@/services/skillsService';
import { SkillSummary } from '@/types/skills';

export function useSkills() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSkills = async () => {
    try {
      setLoading(true);
      const result = await listSkills();
      setSkills(result);
      setError(null);
    } catch (err) {
      console.error('Failed to fetch skills:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch skills');
      setSkills([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSkills();
  }, []);

  return { skills, loading, error, refetch: fetchSkills };
}
