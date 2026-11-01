import { X } from "lucide-react";
import { create } from "zustand";
import { cn } from "@/lib/utils";

interface Toast {
  id: string;
  title?: string;
  description?: string;
  variant?: "default" | "destructive";
}

let toastCount = 0;
const useToasts = create<{ toasts: Toast[] }>(() => ({ toasts: [] }));

function dismiss(id: string) {
  useToasts.setState(({ toasts }) => ({
    toasts: toasts.filter((t) => t.id !== id),
  }));
}

export function toast(props: Omit<Toast, "id">) {
  const id = String(toastCount++);
  useToasts.setState(({ toasts }) => ({
    toasts: [...toasts, { id, ...props }],
  }));
  setTimeout(() => dismiss(id), 5000);
}

export function Toaster() {
  const shown = useToasts((state) => state.toasts);

  return (
    <div
      role="status"
      // The role implies it, but an open Radix dialog hides all but [aria-live] elements from screen readers.
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-0 right-0 z-50 p-4 space-y-2"
    >
      {shown.map((toast) => (
        <div
          key={toast.id}
          className={cn(
            "relative flex items-start gap-3 rounded-lg border p-4 pr-8 shadow-lg",
            "bg-background text-foreground",
            toast.variant === "destructive" &&
              "border-destructive bg-destructive text-destructive-foreground",
          )}
        >
          <div className="flex-1">
            {toast.title && (
              <p className="text-sm font-semibold">{toast.title}</p>
            )}
            {toast.description && (
              <p className="text-sm opacity-90">{toast.description}</p>
            )}
          </div>
          <button
            aria-label="Dismiss notification"
            onClick={() => dismiss(toast.id)}
            className="absolute right-2 top-2 rounded-md p-1 opacity-0 hover:opacity-100 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
