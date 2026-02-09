import { Controller, Control } from 'react-hook-form';
import { Checkbox } from '@/components/ui/checkbox';
import { Agent } from '@/types/config';

export interface CheckboxListItem {
  value: string;
  label: string;
  description?: string;
}

export interface CheckboxListFieldProps {
  name: 'skills' | 'rooms' | 'tools' | 'knowledge_bases';
  control: Control<Agent>;
  items: CheckboxListItem[];
  fieldName: keyof Agent;
  onFieldChange: (fieldName: keyof Agent, value: string[]) => void;
  idPrefix: string;
  emptyMessage?: string;
  className?: string;
}

export function CheckboxListField({
  name,
  control,
  items,
  fieldName,
  onFieldChange,
  idPrefix,
  emptyMessage,
  className = 'space-y-2 max-h-56 overflow-y-auto border rounded-lg p-2',
}: CheckboxListFieldProps) {
  return (
    <Controller
      name={name}
      control={control}
      render={({ field }) => {
        const selected: string[] = field.value ?? [];

        return (
          <div className={className}>
            {items.length === 0 && emptyMessage ? (
              <p className="text-sm text-muted-foreground text-center py-2">{emptyMessage}</p>
            ) : (
              items.map(item => {
                const isChecked = selected.includes(item.value);
                const checkboxId = `${idPrefix}-${item.value}`;
                return (
                  <div
                    key={item.value}
                    className="flex items-center space-x-2 p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200"
                  >
                    <Checkbox
                      id={checkboxId}
                      checked={isChecked}
                      onCheckedChange={checked => {
                        const updated = checked
                          ? [...selected, item.value]
                          : selected.filter(v => v !== item.value);
                        field.onChange(updated);
                        onFieldChange(fieldName, updated);
                      }}
                    />
                    <label htmlFor={checkboxId} className="flex-1 cursor-pointer">
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
      }}
    />
  );
}
