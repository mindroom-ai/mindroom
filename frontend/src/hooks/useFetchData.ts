import { useState, useEffect, useCallback } from 'react';

export function useFetchData<T>(fetcher: () => Promise<T>, defaultValue: T) {
  const [data, setData] = useState<T>(defaultValue);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    try {
      setLoading(true);
      const result = await fetcher();
      setData(result);
      setError(null);
    } catch (err) {
      console.error('Fetch failed:', err);
      setError(err instanceof Error ? err.message : 'Fetch failed');
      setData(defaultValue);
    } finally {
      setLoading(false);
    }
  }, [fetcher, defaultValue]);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { data, loading, error, refetch };
}
