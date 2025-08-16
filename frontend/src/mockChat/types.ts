// Types for mock chat generation

export type Platform = 'matrix' | 'slack' | 'discord' | 'telegram' | 'whatsapp';

export interface User {
  id: string;
  name: string;
  avatar?: string;
  isBot?: boolean;
  color?: string; // For platforms that use colored usernames
}

export interface Message {
  id: string;
  userId: string;
  content: string;
  timestamp: Date;
  isThread?: boolean;
  threadReplies?: number;
  reactions?: Reaction[];
  edited?: boolean;
  platform?: Platform; // Override platform for specific message
}

export interface Reaction {
  emoji: string;
  users: string[];
}

export interface Thread {
  id: string;
  messages: Message[];
  participants: string[];
}

export interface ChatRoom {
  id: string;
  name: string;
  platform: Platform;
  icon?: string;
  description?: string;
}

export interface Conversation {
  room: ChatRoom;
  users: User[];
  messages: Message[];
  threads?: Thread[];
}

export interface ChatTheme {
  platform: Platform;
  backgroundColor: string;
  messageBackgroundSent: string;
  messageBackgroundReceived: string;
  textColorPrimary: string;
  textColorSecondary: string;
  borderColor: string;
  headerBackground: string;
  fontFamily: string;
}
