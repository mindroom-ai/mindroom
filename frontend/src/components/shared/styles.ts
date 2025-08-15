/**
 * Shared style utilities and consistent className patterns for MindRoom widget components
 */

export const sharedStyles = {
  // Panel layouts
  panel: {
    container: 'h-full flex flex-col overflow-hidden',
    header: 'pb-2 sm:pb-3 flex-shrink-0',
    content: 'p-1 sm:p-2 flex-1 overflow-y-auto min-h-0',
  },

  // Header components
  header: {
    titleContainer: 'flex items-center justify-between',
    title: 'text-lg sm:text-xl font-semibold flex items-center gap-2',
    titleWithIcon: 'flex items-center gap-2',
    createButton: 'h-10 sm:h-8 px-3 sm:px-4',
    createButtonCompact: 'h-10 w-10 sm:h-8 sm:w-8 p-0',
  },

  // Search components
  search: {
    container: 'relative mt-2',
    input: 'pl-8 h-10 sm:h-9',
    icon: 'absolute left-2 top-3 sm:top-2.5 h-4 w-4 text-gray-400',
    inputFullWidth: 'w-full',
  },

  // Item components
  item: {
    container:
      'w-full text-left px-3 py-3 sm:py-2 rounded-lg transition-all duration-200 min-h-[44px] sm:min-h-0',
    containerHover: 'hover:bg-gray-100 dark:hover:bg-white/5 hover:shadow-sm',
    containerCard: 'cursor-pointer transition-all hover:shadow-md',
    selected:
      'bg-amber-50 dark:bg-gradient-to-r dark:from-primary/20 dark:to-primary/10 hover:bg-amber-100 dark:hover:from-primary/30 dark:hover:to-primary/20 shadow-sm dark:shadow-lg backdrop-blur-xl',
    selectedCard: 'ring-2 ring-orange-500 bg-gradient-to-r from-orange-500/10 to-amber-500/10',
    iconContainer: 'flex items-center gap-2 transition-all duration-200',
    icon: 'h-4 w-4 transition-colors',
    iconSelected: 'text-primary dark:text-primary',
    iconDefault: 'text-gray-500 dark:text-gray-400',
    content: 'flex-1 min-w-0',
    title: 'font-medium text-sm',
    subtitle: 'text-xs text-gray-500 dark:text-gray-400 truncate',
    cardContent: 'p-3 sm:p-4',
    cardTitle: 'font-medium text-sm',
    cardDescription: 'text-xs text-muted-foreground mt-1',
    badgeContainer: 'flex items-center gap-2 mt-2',
  },

  // Creation forms
  creation: {
    container: 'border-2 border-blue-500',
    containerTeam: 'border-2 border-orange-500',
    inputContainer: 'flex items-center gap-2',
    input: 'flex-1',
    content: 'p-3',
  },

  // Empty states
  empty: {
    container: 'text-center py-8 text-muted-foreground',
    icon: 'h-12 w-12 mx-auto mb-3 opacity-20',
    title: 'text-sm',
    subtitle: 'text-xs mt-1',
  },

  // Lists and containers
  list: {
    container: 'space-y-1',
    containerWithSpacing: 'space-y-2',
  },

  // Badges
  badge: {
    secondary: 'text-xs',
    outline: 'text-xs',
    withIcon: 'h-3 w-3 mr-1',
  },
} as const;

/**
 * Utility function to get consistent selection styles
 */
export function getSelectionStyles(isSelected: boolean, variant: 'button' | 'card' = 'button') {
  if (variant === 'card') {
    return isSelected ? sharedStyles.item.selectedCard : '';
  }
  return isSelected ? sharedStyles.item.selected : '';
}

/**
 * Utility function to get consistent icon styles
 */
export function getIconStyles(isSelected: boolean) {
  return isSelected
    ? `${sharedStyles.item.icon} ${sharedStyles.item.iconSelected}`
    : `${sharedStyles.item.icon} ${sharedStyles.item.iconDefault}`;
}
