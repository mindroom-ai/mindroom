import { createElement } from 'react';
import * as LucideIcons from 'lucide-react';
import * as FaIcons from 'react-icons/fa';
import * as FaIcons6 from 'react-icons/fa6';
import * as SiIcons from 'react-icons/si';
import * as GiIcons from 'react-icons/gi';
import * as VscIcons from 'react-icons/vsc';
import type { LucideIcon } from 'lucide-react';
import type { IconType } from 'react-icons';

// Type for all icon libraries
type IconLibrary = {
  [key: string]: LucideIcon | IconType | any;
};

// Map of icon prefixes to their libraries
const iconLibraries: Record<string, IconLibrary> = {
  // No prefix = Lucide icons
  '': LucideIcons,
  // React Icons libraries
  Fa: { ...FaIcons, ...FaIcons6 },
  Si: SiIcons,
  Gi: GiIcons,
  Vsc: VscIcons,
};

/**
 * Dynamically load an icon component based on its name
 * Supports icons from lucide-react and react-icons libraries
 */
function loadIconComponent(iconName: string): any {
  if (!iconName) return null;

  // Check if it's a prefixed icon (e.g., FaGithub, SiNetflix)
  const prefix = iconName.match(/^(Fa|Si|Gi|Vsc)/)?.[0] || '';
  const library = iconLibraries[prefix] || iconLibraries[''];

  // For react-icons, the full name is the key
  if (prefix) {
    return library[iconName];
  }

  // For Lucide icons, try exact match first
  if (library[iconName]) {
    return library[iconName];
  }

  // Try with 'Icon' suffix for Lucide (some exports include it)
  const withSuffix = iconName + 'Icon';
  if (library[withSuffix]) {
    return library[withSuffix];
  }

  return null;
}

/**
 * Get the appropriate icon for a tool with dynamic loading
 * @param iconName - The icon name from backend (e.g., "FaGithub", "Calculator", "Globe")
 * @param iconColor - The color class from backend (e.g., "text-blue-500")
 * @returns React element of the icon or default Globe icon
 */
export function getIconForTool(
  iconName: string | null,
  iconColor?: string | null
): React.ReactNode {
  // Default icon
  const defaultIcon = createElement(LucideIcons.Globe, {
    className: `h-5 w-5 ${iconColor || ''}`.trim(),
  });

  if (!iconName) {
    return defaultIcon;
  }

  // Try to load the icon component
  const IconComponent = loadIconComponent(iconName);

  if (!IconComponent) {
    console.warn(`Icon "${iconName}" not found, using default`);
    return defaultIcon;
  }

  // Create the icon element with the specified color
  return createElement(IconComponent, {
    className: `h-5 w-5 ${iconColor || ''}`.trim(),
  });
}

// Export specific icons that might be needed elsewhere
export const {
  Calculator,
  Folder,
  Terminal,
  Code,
  Book,
  Globe,
  Search,
  Newspaper,
  TrendingUp,
  FileText,
  Database,
  Mail,
  Film,
  VolumeX,
  Volume2,
} = LucideIcons;
