import { useSyncExternalStore } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

interface Toast {
  id: string;
  title?: string;
  description?: string;
  variant?: "default" | "destructive";
}

let toastCount = 0;
// Replaced on every change, never mutated: React compares snapshots by identity.
let toasts: Toast[] = [];
const listeners = new Set<() => void>();

function publish(next: Toast[]) {
  toasts = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function dismiss(id: string) {
  publish(toasts.filter((t) => t.id !== id));
}

export function toast(props: Omit<Toast, "id">) {
  const id = String(toastCount++);
  publish([...toasts, { id, ...props }]);
  setTimeout(() => dismiss(id), 5000);
}

export function Toaster() {
  const shown = useSyncExternalStore(subscribe, () => toasts);

  return (
    <div className="fixed bottom-0 right-0 z-50 p-4 space-y-2">
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
