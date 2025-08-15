import { toast as toastFn } from './toaster';

interface ToastProps {
  title?: string;
  description?: string;
  variant?: 'default' | 'destructive';
}

export function useToast() {
  const toast = (props: ToastProps) => {
    toastFn(props);
  };

  return { toast };
}
