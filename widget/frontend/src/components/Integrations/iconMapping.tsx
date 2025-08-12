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
  FaAmazon,
  FaApple,
  FaDropbox,
  FaEbay,
  FaFacebook,
  FaGitlab,
  FaGoodreads,
  FaInstagram,
  FaLinkedin,
  FaMicrosoft,
  FaSpotify,
  FaYahoo,
} from 'react-icons/fa';
import { SiHbo, SiNetflix, SiTarget, SiWalmart } from 'react-icons/si';

// Map icon names from backend to React components with their brand colors
export const iconMap: Record<string, React.ReactNode> = {
  // Lucide icons with appropriate colors
  Calculator: <Calculator className="h-5 w-5" />,
  Folder: <Folder className="h-5 w-5" />,
  Terminal: <Terminal className="h-5 w-5" />,
  Code: <Code className="h-5 w-5 text-blue-500" />,
  Book: <Book className="h-5 w-5 text-red-600" />, // arXiv
  Globe: <Globe className="h-5 w-5" />, // Wikipedia and general web
  Search: <Search className="h-5 w-5 text-orange-500" />, // DuckDuckGo
  Newspaper: <Newspaper className="h-5 w-5" />,
  TrendingUp: <TrendingUp className="h-5 w-5 text-green-600" />, // Yahoo Finance
  FileText: <FileText className="h-5 w-5 text-blue-600" />, // CSV
  Database: <Database className="h-5 w-5 text-purple-600" />, // Pandas
  Mail: <Mail className="h-5 w-5" />, // Email SMTP
  Film: <Film className="h-5 w-5 text-yellow-500" />, // IMDb

  // Font Awesome icons with brand colors
  FaGoogle: <FaGoogle className="h-5 w-5" />, // Google Search and Gmail
  FaTwitter: <FaTwitter className="h-5 w-5 text-blue-400" />,
  FaReddit: <FaReddit className="h-5 w-5 text-orange-600" />,
  FaTelegram: <FaTelegram className="h-5 w-5 text-blue-500" />,
  FaGithub: <FaGithub className="h-5 w-5" />,
  FaDocker: <FaDocker className="h-5 w-5 text-blue-400" />,
  FaSlack: <FaSlack className="h-5 w-5 text-purple-600" />,
  FaYoutube: <FaYoutube className="h-5 w-5 text-red-600" />,
  FaAmazon: <FaAmazon className="h-5 w-5 text-orange-500" />,
  FaApple: <FaApple className="h-5 w-5 text-gray-800" />,
  FaDropbox: <FaDropbox className="h-5 w-5 text-blue-600" />,
  FaEbay: <FaEbay className="h-5 w-5 text-blue-500" />,
  FaFacebook: <FaFacebook className="h-5 w-5 text-blue-600" />,
  FaGitlab: <FaGitlab className="h-5 w-5 text-orange-600" />,
  FaGoodreads: <FaGoodreads className="h-5 w-5 text-amber-700" />,
  FaInstagram: <FaInstagram className="h-5 w-5 text-pink-600" />,
  FaLinkedin: <FaLinkedin className="h-5 w-5 text-blue-700" />,
  FaMicrosoft: <FaMicrosoft className="h-5 w-5 text-blue-600" />,
  FaSpotify: <FaSpotify className="h-5 w-5 text-green-500" />,
  FaYahoo: <FaYahoo className="h-5 w-5 text-purple-600" />,

  // Simple Icons with brand colors
  SiHbo: <SiHbo className="h-5 w-5 text-purple-600" />,
  SiNetflix: <SiNetflix className="h-5 w-5 text-red-600" />,
  SiTarget: <SiTarget className="h-5 w-5 text-red-600" />,
  SiWalmart: <SiWalmart className="h-5 w-5 text-blue-500" />,

  // Additional mappings for tools that might use different icon names
  'Search-indigo': <Search className="h-5 w-5 text-indigo-600" />, // Tavily
  'FileText-purple': <FileText className="h-5 w-5 text-purple-500" />, // Jina
  'Globe-blue': <Globe className="h-5 w-5 text-blue-600" />, // Website reader
};

export function getIconForTool(iconName: string | null): React.ReactNode {
  if (!iconName) {
    return <Globe className="h-5 w-5" />; // Default icon
  }
  return iconMap[iconName] || <Globe className="h-5 w-5" />;
}
