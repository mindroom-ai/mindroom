/**
 * Shared style utilities and consistent className patterns for dashboard components
 */

export const sharedStyles = {
  // Panel layouts
  panel: {
    container: "glass-panel h-full flex flex-col overflow-hidden rounded-xl",
    header: "px-3 pt-3 pb-2 sm:px-4 sm:pt-4 flex-shrink-0",
    content: "px-2 pb-2 sm:px-3 sm:pb-3 flex-1 overflow-y-auto min-h-0",
  },

  // Header components
  header: {
    titleContainer: "flex items-center justify-between",
    title: "text-base sm:text-lg font-semibold flex items-center gap-2",
    titleWithIcon: "flex items-center gap-2",
    createButton: "h-10 sm:h-8 px-3 sm:px-4",
    createButtonCompact: "h-10 w-10 sm:h-8 sm:w-8 p-0",
  },

  // Search components
  search: {
    container: "relative mt-2",
    input: "pl-8 h-10 sm:h-9",
    icon: "absolute left-2 top-3 sm:top-2.5 h-4 w-4 text-muted-foreground",
    inputFullWidth: "w-full",
  },

  // Item components
  item: {
    containerCard:
      "cursor-pointer rounded-lg outline-none transition-colors duration-150 hover:bg-secondary focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
    selectedCard: "bg-active hover:bg-active",
    cardContent: "p-3",
    cardTitle: "font-medium text-sm",
    cardDescription: "text-xs text-muted-foreground mt-1",
    badgeContainer: "flex flex-wrap items-center gap-1.5 mt-1.5",
  },

  // Creation forms
  creation: {
    container: "border-input",
    inputContainer: "flex items-center gap-2",
    input: "flex-1",
    content: "p-3",
  },

  // Empty states
  empty: {
    container: "text-center py-6 text-muted-foreground",
    icon: "h-10 w-10 mx-auto mb-2 opacity-20",
    title: "text-sm",
    subtitle: "text-xs mt-1",
  },

  // Lists and containers
  list: {
    container: "space-y-1",
    containerWithSpacing: "space-y-1",
  },

  // Badges
  badge: {
    secondary: "text-xs",
    outline: "text-xs",
    withIcon: "h-3 w-3 mr-1",
  },
} as const;
