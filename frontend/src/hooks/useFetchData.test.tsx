import { renderHook, waitFor, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useFetchData } from './useFetchData';

describe('useFetchData', () => {
  it('fails closed and ignores stale responses when the fetcher changes', async () => {
    let resolveFirstFetch: ((value: string) => void) | undefined;
    let resolveSecondFetch: ((value: string) => void) | undefined;
    const firstFetcher = vi.fn(
      () =>
        new Promise<string>(resolve => {
          resolveFirstFetch = resolve;
        })
    );
    const secondFetcher = vi.fn(
      () =>
        new Promise<string>(resolve => {
          resolveSecondFetch = resolve;
        })
    );

    const { result, rerender } = renderHook(
      ({ fetcher }: { fetcher: () => Promise<string> }) => useFetchData(fetcher, 'default'),
      { initialProps: { fetcher: firstFetcher } }
    );

    await waitFor(() => expect(firstFetcher).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBe('default');
    expect(result.current.loading).toBe(true);

    rerender({ fetcher: secondFetcher });

    await waitFor(() => expect(secondFetcher).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBe('default');
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();

    await act(async () => {
      resolveFirstFetch?.('stale');
    });

    expect(result.current.data).toBe('default');
    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveSecondFetch?.('fresh');
    });

    await waitFor(() => {
      expect(result.current.data).toBe('fresh');
      expect(result.current.loading).toBe(false);
      expect(result.current.error).toBeNull();
    });
  });

  it('ignores stale errors when the fetcher changes', async () => {
    let rejectFirstFetch: ((reason?: unknown) => void) | undefined;
    let resolveSecondFetch: ((value: string) => void) | undefined;
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const firstFetcher = vi.fn(
      () =>
        new Promise<string>((_, reject) => {
          rejectFirstFetch = reject;
        })
    );
    const secondFetcher = vi.fn(
      () =>
        new Promise<string>(resolve => {
          resolveSecondFetch = resolve;
        })
    );

    const { result, rerender } = renderHook(
      ({ fetcher }: { fetcher: () => Promise<string> }) => useFetchData(fetcher, 'default'),
      { initialProps: { fetcher: firstFetcher } }
    );

    await waitFor(() => expect(firstFetcher).toHaveBeenCalledTimes(1));

    rerender({ fetcher: secondFetcher });

    await waitFor(() => expect(secondFetcher).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBe('default');
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();

    await act(async () => {
      rejectFirstFetch?.(new Error('stale failure'));
    });

    expect(result.current.data).toBe('default');
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();
    expect(consoleErrorSpy).not.toHaveBeenCalled();

    await act(async () => {
      resolveSecondFetch?.('fresh');
    });

    await waitFor(() => {
      expect(result.current.data).toBe('fresh');
      expect(result.current.loading).toBe(false);
      expect(result.current.error).toBeNull();
    });

    consoleErrorSpy.mockRestore();
  });

  it('ignores stale refetch callbacks after the fetcher changes', async () => {
    const firstFetcher = vi.fn().mockResolvedValue('shared');
    const secondFetcher = vi.fn().mockResolvedValue('scoped');

    const { result, rerender } = renderHook(
      ({ fetcher }: { fetcher: () => Promise<string> }) => useFetchData(fetcher, 'default'),
      { initialProps: { fetcher: firstFetcher } }
    );

    await waitFor(() => {
      expect(result.current.data).toBe('shared');
      expect(result.current.loading).toBe(false);
    });

    const staleRefetch = result.current.refetch;

    rerender({ fetcher: secondFetcher });

    await waitFor(() => {
      expect(result.current.data).toBe('scoped');
      expect(result.current.loading).toBe(false);
      expect(result.current.error).toBeNull();
    });

    await act(async () => {
      await staleRefetch();
    });

    expect(firstFetcher).toHaveBeenCalledTimes(1);
    expect(secondFetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('scoped');
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });
});
