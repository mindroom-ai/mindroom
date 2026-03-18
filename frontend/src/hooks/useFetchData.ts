import { useState, useEffect, useCallback, useRef } from 'react';

type FetchState<T> = {
  data: T;
  fetcher: () => Promise<T>;
};

export function useFetchData<T>(fetcher: () => Promise<T>, defaultValue: T) {
  const [state, setState] = useState<FetchState<T>>({
    data: defaultValue,
    fetcher,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);
  const currentFetcherRef = useRef(fetcher);
  currentFetcherRef.current = fetcher;

  const refetch = useCallback(async () => {
    const isCurrentFetcher = () => currentFetcherRef.current === fetcher;

    if (!isCurrentFetcher()) {
      return;
    }

    const requestId = ++requestIdRef.current;
    try {
      setLoading(true);
      setError(null);
      const result = await fetcher();
      if (requestId !== requestIdRef.current || !isCurrentFetcher()) {
        return;
      }
      setState({ data: result, fetcher });
    } catch (err) {
      if (requestId !== requestIdRef.current || !isCurrentFetcher()) {
        return;
      }
      console.error('Fetch failed:', err);
      setError(err instanceof Error ? err.message : 'Fetch failed');
      setState({ data: defaultValue, fetcher });
    } finally {
      if (requestId === requestIdRef.current && isCurrentFetcher()) {
        setLoading(false);
      }
    }
  }, [fetcher, defaultValue]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const hasFreshData = state.fetcher === fetcher;

  return {
    data: hasFreshData ? state.data : defaultValue,
    loading: loading || !hasFreshData,
    error: hasFreshData ? error : null,
    refetch,
  };
}
