import {
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
} from 'lucide-react';
import {
  FaGoogle,
  FaTwitter,
  FaReddit,
  FaTelegram,
  FaGithub,
  FaDocker,
  FaSlack,
  FaYoutube,
} from 'react-icons/fa';

// Map icon names from backend to React components
export const iconMap: Record<string, React.ReactNode> = {
  // Lucide icons
  Calculator: <Calculator className="h-5 w-5" />,
  Folder: <Folder className="h-5 w-5" />,
  Terminal: <Terminal className="h-5 w-5" />,
  Code: <Code className="h-5 w-5" />,
  Book: <Book className="h-5 w-5" />,
  Globe: <Globe className="h-5 w-5" />,
  Search: <Search className="h-5 w-5" />,
  Newspaper: <Newspaper className="h-5 w-5" />,
  TrendingUp: <TrendingUp className="h-5 w-5" />,
  FileText: <FileText className="h-5 w-5" />,
  Database: <Database className="h-5 w-5" />,
  Mail: <Mail className="h-5 w-5" />,
  Film: <Film className="h-5 w-5" />,

  // Font Awesome icons
  FaGoogle: <FaGoogle className="h-5 w-5" />,
  FaTwitter: <FaTwitter className="h-5 w-5 text-blue-400" />,
  FaReddit: <FaReddit className="h-5 w-5 text-orange-600" />,
  FaTelegram: <FaTelegram className="h-5 w-5 text-blue-500" />,
  FaGithub: <FaGithub className="h-5 w-5" />,
  FaDocker: <FaDocker className="h-5 w-5 text-blue-400" />,
  FaSlack: <FaSlack className="h-5 w-5" />,
  FaYoutube: <FaYoutube className="h-5 w-5 text-red-600" />,
};

export function getIconForTool(iconName: string | null): React.ReactNode {
  if (!iconName) {
    return <Globe className="h-5 w-5" />; // Default icon
  }
  return iconMap[iconName] || <Globe className="h-5 w-5" />;
}
