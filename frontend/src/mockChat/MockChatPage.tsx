import React from 'react';
import { MockChat } from './MockChat';
import { Conversation } from './types';

// This component renders a full page for screenshot capture
export const MockChatPage: React.FC = () => {
  // Get conversation data from URL params or window object
  const getConversationData = (): Conversation | null => {
    // Check if data is passed via window object (for programmatic use)
    if (window.mockChatData) {
      return window.mockChatData;
    }

    // Try to parse from URL params
    const params = new URLSearchParams(window.location.search);
    const data = params.get('data');
    if (data) {
      try {
        return JSON.parse(decodeURIComponent(data));
      } catch (e) {
        console.error('Failed to parse conversation data:', e);
      }
    }

    return null;
  };

  const conversation = getConversationData();

  if (!conversation) {
    return (
      <div style={{ padding: '20px', fontFamily: 'sans-serif' }}>
        <h1>Mock Chat Preview</h1>
        <p>No conversation data provided.</p>
        <p>Pass data via URL parameter: ?data=...</p>
      </div>
    );
  }

  // Parse dates from JSON
  const parsedConversation: Conversation = {
    ...conversation,
    messages: conversation.messages.map(msg => ({
      ...msg,
      timestamp: new Date(msg.timestamp),
    })),
  };

  return (
    <div
      style={{
        padding: '20px',
        background: '#f0f0f0',
        minHeight: '100vh',
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
      }}
    >
      <MockChat conversation={parsedConversation} width={800} height={600} />
    </div>
  );
};

// Add type declaration for window
declare global {
  interface Window {
    mockChatData?: Conversation;
  }
}
