import { Conversation } from '../types';

// Example from README: Monday in Matrix room
export const matrixExample: Conversation = {
  room: {
    id: 'mindroom-main',
    name: 'MindRoom Development',
    platform: 'matrix',
    icon: '💬',
  },
  users: [
    {
      id: 'user1',
      name: 'You',
      color: '#0dbd8b',
    },
    {
      id: 'assistant',
      name: 'mindroom_assistant',
      isBot: true,
      color: '#5865f2',
    },
  ],
  messages: [
    {
      id: 'm1',
      userId: 'user1',
      content: '@assistant Remember our project uses Python 3.11 and FastAPI',
      timestamp: new Date('2024-01-15T10:00:00'),
      isThread: false,
    },
    {
      id: 'm2',
      userId: 'assistant',
      content:
        "Got it! I'll remember that your project uses Python 3.11 and FastAPI. This will help me provide more accurate assistance with compatible code examples and dependencies.",
      timestamp: new Date('2024-01-15T10:00:15'),
      isThread: false,
    },
  ],
};

// Example from README: Tuesday in Slack
export const slackExample: Conversation = {
  room: {
    id: 'general',
    name: 'general',
    platform: 'slack',
  },
  users: [
    {
      id: 'colleague',
      name: 'Sarah Chen',
      color: '#e01e5a',
    },
    {
      id: 'user1',
      name: 'You',
      color: '#2eb67d',
    },
    {
      id: 'assistant',
      name: 'mindroom_assistant',
      isBot: true,
      color: '#5865f2',
    },
  ],
  messages: [
    {
      id: 's1',
      userId: 'colleague',
      content: 'What Python version are we using?',
      timestamp: new Date('2024-01-16T14:30:00'),
      isThread: false,
    },
    {
      id: 's2',
      userId: 'user1',
      content: '@assistant can you help?',
      timestamp: new Date('2024-01-16T14:30:30'),
      isThread: false,
    },
    {
      id: 's3',
      userId: 'assistant',
      content: "[Joins from Matrix] We're using Python 3.11 with FastAPI",
      timestamp: new Date('2024-01-16T14:30:45'),
      isThread: false,
      reactions: [{ emoji: '✅', users: ['colleague', 'user1'] }],
    },
  ],
};

// Example from README: Cross-organization collaboration
export const discordCollabExample: Conversation = {
  room: {
    id: 'project-review',
    name: 'project-review',
    platform: 'discord',
    description: 'Architecture review channel',
  },
  users: [
    {
      id: 'client',
      name: 'Alex (Client)',
      color: '#7289da',
    },
    {
      id: 'user1',
      name: 'You',
      color: '#43b581',
    },
    {
      id: 'assistant',
      name: 'mindroom_assistant',
      isBot: true,
      color: '#5865f2',
    },
    {
      id: 'architect',
      name: 'client_architect_ai',
      isBot: true,
      color: '#e91e63',
    },
  ],
  messages: [
    {
      id: 'd1',
      userId: 'client',
      content: 'Can our architect AI review this with your team?',
      timestamp: new Date('2024-01-18T10:00:00'),
      isThread: false,
    },
    {
      id: 'd2',
      userId: 'user1',
      content: 'Sure! @assistant please collaborate with them',
      timestamp: new Date('2024-01-18T10:00:30'),
      isThread: false,
    },
    {
      id: 'd3',
      userId: 'assistant',
      content:
        '[Joins from your Matrix server] Ready to review the architecture. I have context about our FastAPI patterns and Python 3.11 requirements.',
      timestamp: new Date('2024-01-18T10:01:00'),
      isThread: false,
    },
    {
      id: 'd4',
      userId: 'architect',
      content:
        "[Joins from client's server] Excellent. I'll share our microservices patterns and scaling requirements.",
      timestamp: new Date('2024-01-18T10:01:30'),
      isThread: false,
    },
    {
      id: 'd5',
      userId: 'assistant',
      content:
        'Based on your scaling needs and our FastAPI expertise, I recommend implementing async endpoints with Redis caching for the high-traffic services.',
      timestamp: new Date('2024-01-18T10:02:00'),
      isThread: false,
    },
    {
      id: 'd6',
      userId: 'architect',
      content:
        'Agreed. That aligns with our infrastructure. We can provide Kubernetes deployment configs optimized for these patterns.',
      timestamp: new Date('2024-01-18T10:02:30'),
      isThread: false,
      reactions: [
        { emoji: '🚀', users: ['client', 'user1'] },
        { emoji: '💯', users: ['assistant'] },
      ],
    },
  ],
};

// Example: Multi-agent collaboration
export const multiAgentExample: Conversation = {
  room: {
    id: 'analysis-room',
    name: 'Competitive Analysis',
    platform: 'matrix',
  },
  users: [
    {
      id: 'user1',
      name: 'You',
      color: '#0dbd8b',
    },
    {
      id: 'research',
      name: 'mindroom_research',
      isBot: true,
      color: '#9b59b6',
    },
    {
      id: 'analyst',
      name: 'mindroom_analyst',
      isBot: true,
      color: '#3498db',
    },
    {
      id: 'writer',
      name: 'mindroom_writer',
      isBot: true,
      color: '#e74c3c',
    },
  ],
  messages: [
    {
      id: 'ma1',
      userId: 'user1',
      content: '@research @analyst @writer Create a competitive analysis report',
      timestamp: new Date('2024-01-19T09:00:00'),
      isThread: false,
    },
    {
      id: 'ma2',
      userId: 'research',
      content: "I'll gather data on our top 5 competitors...",
      timestamp: new Date('2024-01-19T09:00:30'),
      isThread: false,
    },
    {
      id: 'ma3',
      userId: 'analyst',
      content: "I'll identify strategic patterns and opportunities...",
      timestamp: new Date('2024-01-19T09:00:45'),
      isThread: false,
    },
    {
      id: 'ma4',
      userId: 'writer',
      content: "I'll compile everything into an executive summary...",
      timestamp: new Date('2024-01-19T09:01:00'),
      isThread: false,
    },
    {
      id: 'ma5',
      userId: 'research',
      content: '✅ Research complete. Found 3 key market gaps our competitors are missing.',
      timestamp: new Date('2024-01-19T09:15:00'),
      isThread: true,
      threadReplies: 5,
    },
    {
      id: 'ma6',
      userId: 'analyst',
      content: '📊 Analysis ready. We have a 6-month window to capture 15% market share.',
      timestamp: new Date('2024-01-19T09:20:00'),
      isThread: true,
      threadReplies: 3,
    },
    {
      id: 'ma7',
      userId: 'writer',
      content:
        '📝 Executive summary delivered. Key finding: We can differentiate through federation - something no competitor offers.',
      timestamp: new Date('2024-01-19T09:25:00'),
      isThread: false,
      reactions: [
        { emoji: '🎯', users: ['user1'] },
        { emoji: '💡', users: ['research', 'analyst'] },
      ],
    },
  ],
};

// Example: Telegram client interaction
export const telegramExample: Conversation = {
  room: {
    id: 'client-support',
    name: 'Tech Support Group',
    platform: 'telegram',
  },
  users: [
    {
      id: 'client',
      name: 'Client Team',
      color: '#0088cc',
    },
    {
      id: 'user1',
      name: 'You',
    },
    {
      id: 'assistant',
      name: 'mindroom_assistant',
      isBot: true,
    },
  ],
  messages: [
    {
      id: 't1',
      userId: 'client',
      content: 'Can your AI review our API spec?',
      timestamp: new Date('2024-01-17T11:00:00'),
      isThread: false,
    },
    {
      id: 't2',
      userId: 'user1',
      content: '@assistant please analyze this',
      timestamp: new Date('2024-01-17T11:00:30'),
      isThread: false,
    },
    {
      id: 't3',
      userId: 'assistant',
      content:
        "[Travels from your server] I'll review this against our FastAPI patterns and provide feedback on REST design, authentication flow, and performance considerations.",
      timestamp: new Date('2024-01-17T11:01:00'),
      isThread: false,
    },
  ],
};

// WhatsApp example
export const whatsappExample: Conversation = {
  room: {
    id: 'team-chat',
    name: 'Dev Team',
    platform: 'whatsapp',
  },
  users: [
    {
      id: 'user1',
      name: 'You',
    },
    {
      id: 'teammate',
      name: 'John',
    },
    {
      id: 'assistant',
      name: 'MindRoom Assistant',
      isBot: true,
    },
  ],
  messages: [
    {
      id: 'w1',
      userId: 'teammate',
      content: 'Hey, what was that Python package we discussed for async tasks?',
      timestamp: new Date('2024-01-20T16:45:00'),
      isThread: false,
    },
    {
      id: 'w2',
      userId: 'user1',
      content: '@assistant can you remind us?',
      timestamp: new Date('2024-01-20T16:45:30'),
      isThread: false,
    },
    {
      id: 'w3',
      userId: 'assistant',
      content:
        'Based on our previous discussions, you were considering Celery for distributed task queues with Redis as the broker. Perfect for your FastAPI async requirements.',
      timestamp: new Date('2024-01-20T16:46:00'),
      isThread: false,
    },
    {
      id: 'w4',
      userId: 'teammate',
      content: 'Right! Thanks 👍',
      timestamp: new Date('2024-01-20T16:46:30'),
      isThread: false,
    },
  ],
};

// Export all examples
export const examples = {
  matrix: matrixExample,
  slack: slackExample,
  discord: discordCollabExample,
  multiAgent: multiAgentExample,
  telegram: telegramExample,
  whatsapp: whatsappExample,
};
