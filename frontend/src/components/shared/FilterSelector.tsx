import { cn } from '@/lib/utils';
import { ReactNode } from 'react';
import { Filter } from 'lucide-react';

interface FilterOption {
  value: string;
  label: string | ReactNode;
  count?: number;
  showIcon?: boolean;
  icon?: ReactNode;
}

interface FilterSelectorProps {
  options: FilterOption[];
  value: string | string[];
  onChange: (value: string | string[]) => void;
  multiple?: boolean;
  className?: string;
  size?: 'sm' | 'md' | 'lg';
  showFilterIcon?: boolean;
}

export function FilterSelector({
  options,
  value,
  onChange,
  multiple = false,
  className,
  size = 'md',
  showFilterIcon = false,
}: FilterSelectorProps) {
  const sizeClasses = {
    sm: 'px-2.5 py-1 text-xs',
    md: 'px-3 py-1.5 text-sm',
    lg: 'px-4 py-2 text-base',
  };

  const handleClick = (optionValue: string) => {
    if (multiple) {
      const currentValues = Array.isArray(value) ? value : [value];
      const newValues = currentValues.includes(optionValue)
        ? currentValues.filter(v => v !== optionValue)
        : [...currentValues, optionValue];
      onChange(newValues);
    } else {
      onChange(optionValue);
    }
  };

  const isSelected = (optionValue: string) => {
    if (multiple) {
      return Array.isArray(value) ? value.includes(optionValue) : value === optionValue;
    }
    return value === optionValue;
  };

  return (
    <div
      className={cn(
        'inline-flex gap-1 rounded-lg p-1.5',
        'bg-white dark:bg-stone-900/70',
        'backdrop-blur-xl',
        'border border-gray-200 dark:border-white/10',
        className
      )}
    >
      {options.map(option => (
        <button
          key={option.value}
          onClick={() => handleClick(option.value)}
          className={cn(
            'relative rounded-md font-medium transition-all duration-200',
            'hover:bg-gray-100 dark:hover:bg-white/5',
            sizeClasses[size],
            isSelected(option.value) && [
              'bg-amber-500/20 dark:bg-amber-500/20',
              'text-amber-900 dark:text-amber-200',
              'shadow-sm',
              'hover:bg-amber-500/30 dark:hover:bg-amber-500/30',
            ],
            !isSelected(option.value) && [
              'text-gray-600 dark:text-gray-400',
              'hover:text-gray-900 dark:hover:text-gray-200',
            ]
          )}
        >
          <span className="flex items-center gap-1.5">
            {option.icon
              ? option.icon
              : (showFilterIcon || option.showIcon) && (
                  <Filter
                    className={cn(
                      'w-3.5 h-3.5',
                      isSelected(option.value) && 'text-amber-600 dark:text-amber-400'
                    )}
                  />
                )}
            {option.label}
            {option.count !== undefined && (
              <span
                className={cn(
                  'inline-flex items-center justify-center',
                  'min-w-[1.25rem] h-5 px-1 rounded-full',
                  'text-xs font-medium',
                  isSelected(option.value)
                    ? 'bg-amber-600/30 dark:bg-amber-400/30 text-amber-900 dark:text-amber-200'
                    : 'bg-gray-600/20 dark:bg-gray-600/30 text-gray-700 dark:text-gray-400'
                )}
              >
                {option.count}
              </span>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}
