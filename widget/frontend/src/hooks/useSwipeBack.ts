import { useEffect, useRef } from 'react';

interface SwipeBackOptions {
  onSwipeBack: () => void;
  threshold?: number;
  enabled?: boolean;
}

/**
 * Hook to detect swipe-right gesture for back navigation on mobile
 */
export function useSwipeBack({ onSwipeBack, threshold = 50, enabled = true }: SwipeBackOptions) {
  const touchStartX = useRef<number | null>(null);
  const touchStartY = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) return;

    const handleTouchStart = (e: TouchEvent) => {
      touchStartX.current = e.touches[0].clientX;
      touchStartY.current = e.touches[0].clientY;
    };

    const handleTouchEnd = (e: TouchEvent) => {
      if (touchStartX.current === null || touchStartY.current === null) return;

      const touchEndX = e.changedTouches[0].clientX;
      const touchEndY = e.changedTouches[0].clientY;

      const deltaX = touchEndX - touchStartX.current;
      const deltaY = Math.abs(touchEndY - touchStartY.current);

      // Check if it's a horizontal swipe (not vertical scroll)
      // and if it's from left edge of screen
      if (deltaX > threshold && deltaY < 50 && touchStartX.current < 50) {
        onSwipeBack();
      }

      touchStartX.current = null;
      touchStartY.current = null;
    };

    document.addEventListener('touchstart', handleTouchStart);
    document.addEventListener('touchend', handleTouchEnd);

    return () => {
      document.removeEventListener('touchstart', handleTouchStart);
      document.removeEventListener('touchend', handleTouchEnd);
    };
  }, [onSwipeBack, threshold, enabled]);
}
