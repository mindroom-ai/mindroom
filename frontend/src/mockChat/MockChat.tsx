import React from 'react';
import { Conversation, Message, User } from './types';
import { getTheme } from './themes';
import './MockChat.css';

interface MockChatProps {
  conversation: Conversation;
  width?: number;
  height?: number;
  showHeader?: boolean;
  className?: string;
}

export const MockChat: React.FC<MockChatProps> = ({
  conversation,
  width = 800,
  height = 600,
  showHeader = true,
  className = '',
}) => {
  const theme = getTheme(conversation.room.platform);

  const getUserById = (userId: string): User | undefined => {
    return conversation.users.find(u => u.id === userId);
  };

  const formatTime = (date: Date): string => {
    return new Intl.DateTimeFormat('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }).format(date);
  };

  const renderAvatar = (user: User | undefined) => {
    if (!user) return null;

    const initial = user.name.charAt(0).toUpperCase();
    const avatarClass = `avatar avatar-${conversation.room.platform}`;

    if (user.avatar) {
      return <img src={user.avatar} alt={user.name} className={avatarClass} />;
    }

    return (
      <div className={avatarClass} style={{ backgroundColor: user.color || '#667eea' }}>
        {initial}
      </div>
    );
  };

  const renderMessage = (message: Message, index: number) => {
    const user = getUserById(message.userId);
    const prevMessage = index > 0 ? conversation.messages[index - 1] : null;
    const showUserInfo = !prevMessage || prevMessage.userId !== message.userId;

    return (
      <div
        key={message.id}
        className={`message message-${conversation.room.platform} ${
          showUserInfo ? 'message-with-header' : ''
        }`}
      >
        {showUserInfo && (
          <div className="message-header">
            {renderAvatar(user)}
            <div className="message-meta">
              <span className="username" style={{ color: user?.color }}>
                {user?.name}
                {user?.isBot && <span className="bot-badge">BOT</span>}
              </span>
              <span className="timestamp">{formatTime(message.timestamp)}</span>
            </div>
          </div>
        )}

        <div className={`message-content ${!showUserInfo ? 'message-content-continued' : ''}`}>
          {!showUserInfo && <div className="message-spacer" />}
          <div className="message-text">
            {message.content}
            {message.edited && <span className="edited-badge">(edited)</span>}
          </div>
        </div>

        {message.reactions && message.reactions.length > 0 && (
          <div className="message-reactions">
            {message.reactions.map((reaction, i) => (
              <div key={i} className="reaction">
                <span className="reaction-emoji">{reaction.emoji}</span>
                <span className="reaction-count">{reaction.users.length}</span>
              </div>
            ))}
          </div>
        )}

        {message.isThread && (
          <div className="thread-indicator">
            <span className="thread-replies">{message.threadReplies || 0} replies</span>
          </div>
        )}
      </div>
    );
  };

  const renderPlatformHeader = () => {
    switch (conversation.room.platform) {
      case 'slack':
        return (
          <div className="header-content">
            <span className="channel-hash">#</span>
            <span className="channel-name">{conversation.room.name}</span>
          </div>
        );
      case 'discord':
        return (
          <div className="header-content">
            <span className="channel-hash">#</span>
            <span className="channel-name">{conversation.room.name}</span>
            {conversation.room.description && (
              <span className="channel-description">{conversation.room.description}</span>
            )}
          </div>
        );
      case 'matrix':
        return (
          <div className="header-content">
            <span className="room-icon">💬</span>
            <span className="room-name">{conversation.room.name}</span>
          </div>
        );
      case 'telegram':
        return (
          <div className="header-content">
            <span className="chat-name">{conversation.room.name}</span>
            <span className="member-count">{conversation.users.length} members</span>
          </div>
        );
      case 'whatsapp':
        return (
          <div className="header-content">
            <span className="chat-name">{conversation.room.name}</span>
          </div>
        );
      default:
        return (
          <div className="header-content">
            <span className="room-name">{conversation.room.name}</span>
          </div>
        );
    }
  };

  return (
    <div
      className={`mock-chat mock-chat-${conversation.room.platform} ${className}`}
      style={{
        width,
        height,
        backgroundColor: theme.backgroundColor,
        color: theme.textColorPrimary,
        fontFamily: theme.fontFamily,
      }}
    >
      {showHeader && (
        <div
          className="chat-header"
          style={{
            backgroundColor: theme.headerBackground,
            borderBottom: `1px solid ${theme.borderColor}`,
          }}
        >
          {renderPlatformHeader()}
        </div>
      )}

      <div className="chat-messages">
        {conversation.messages.map((message, index) => renderMessage(message, index))}
      </div>

      <div className="chat-input" style={{ borderTop: `1px solid ${theme.borderColor}` }}>
        <input type="text" placeholder={`Message ${conversation.room.name}`} disabled />
      </div>
    </div>
  );
};
