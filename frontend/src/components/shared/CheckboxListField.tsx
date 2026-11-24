import { Checkbox } from "@/components/ui/checkbox";

export interface CheckboxListItem {
  value: string;
  label: string;
  description?: string;
}

export interface CheckboxListFieldProps {
  items: CheckboxListItem[];
  selected: string[];
  onChange: (selected: string[]) => void;
  idPrefix: string;
  emptyMessage: string;
  className?: string;
}

export function CheckboxListField({
  items,
  selected,
  onChange,
  idPrefix,
  emptyMessage,
  className = "space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2",
}: CheckboxListFieldProps) {
  return (
    <div className={className}>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-2">
          {emptyMessage}
        </p>
      ) : (
        items.map((item) => {
          const isChecked = selected.includes(item.value);
          const checkboxId = `${idPrefix}-${item.value}`;
          return (
            <div
              key={item.value}
              className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-secondary transition-all duration-200"
            >
              <Checkbox
                id={checkboxId}
                checked={isChecked}
                onCheckedChange={(checked) =>
                  onChange(
                    checked
                      ? [...selected, item.value]
                      : selected.filter((v) => v !== item.value),
                  )
                }
                className="h-5 w-5 sm:h-4 sm:w-4"
              />
              <label
                htmlFor={checkboxId}
                className="flex-1 cursor-pointer select-none"
              >
                <div className="font-medium text-sm">{item.label}</div>
                {item.description && (
                  <div className="text-xs text-gray-500 dark:text-gray-400">
                    {item.description}
                  </div>
                )}
              </label>
            </div>
          );
        })
      )}
    </div>
  );
}
