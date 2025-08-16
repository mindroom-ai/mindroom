const { generateMockupFromJSON } = require('./generate-mockup-from-json.cjs');

// Conversation 1: Cross-platform memory
const crossPlatformConvo = {
  title: "MindRoom Cross-Platform Memory",
  messages: [
    {
      id: 1,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "@assistant Remember our project uses Python 3.11 and FastAPI",
      timestamp: "Monday 2:30 PM",
      mentions: ["assistant"]
    },
    {
      id: 2,
      user: {
        name: "Colleague",
        avatar: { type: "initial", value: "C", color: "#2196f3" }
      },
      content: "What Python version are we using?",
      timestamp: "Tuesday 10:15 AM"
    },
    {
      id: 3,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "@assistant can you help?",
      timestamp: "Tuesday 10:16 AM",
      mentions: ["assistant"]
    },
    {
      id: 4,
      user: {
        name: "MindRoom Assistant",
        avatar: { type: "image", value: "../../avatars/agents/general.png" },
        badge: { text: "AI Agent", type: "primary" }
      },
      content: "[Joins from Matrix] We're using Python 3.11 with FastAPI",
      timestamp: "Tuesday 10:16 AM"
    },
    {
      id: 5,
      user: {
        name: "Client",
        avatar: { type: "initial", value: "CL", color: "#9c27b0" }
      },
      content: "Can your AI review our API spec?",
      timestamp: "Wednesday 3:45 PM"
    },
    {
      id: 6,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "@assistant please analyze this",
      timestamp: "Wednesday 3:46 PM",
      mentions: ["assistant"]
    },
    {
      id: 7,
      user: {
        name: "MindRoom Assistant",
        avatar: { type: "image", value: "../../avatars/agents/general.png" },
        badge: { text: "AI Agent", type: "primary" }
      },
      content: "[Travels from your server] I'll review this against our FastAPI patterns...",
      timestamp: "Wednesday 3:46 PM"
    }
  ]
};

// Conversation 2: Cross-organization collaboration
const crossOrgCollab = {
  title: "Cross-Organization AI Collaboration",
  messages: [
    {
      id: 1,
      user: {
        name: "Client",
        avatar: { type: "initial", value: "C", color: "#e91e63" }
      },
      content: "Can our architect AI review this with your team?",
      timestamp: "Thursday 2:00 PM"
    },
    {
      id: 2,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "Sure! @assistant please collaborate with them",
      timestamp: "Thursday 2:01 PM",
      mentions: ["assistant"]
    },
    {
      id: 3,
      user: {
        name: "Your Assistant",
        avatar: { type: "image", value: "../../avatars/agents/general.png" },
        badge: { text: "AI Agent", type: "primary" }
      },
      content: "[Joins from your Matrix server] Ready to review the architecture. I have context on our FastAPI patterns and microservices design.",
      timestamp: "Thursday 2:01 PM"
    },
    {
      id: 4,
      user: {
        name: "Client's Architect AI",
        avatar: { type: "image", value: "../../avatars/agents/code.png" },
        badge: { text: "External AI", type: "external" }
      },
      content: "[Joins from their server] Excellent! I'll share our deployment patterns and scaling requirements.",
      timestamp: "Thursday 2:02 PM"
    },
    {
      id: 5,
      user: {
        name: "Your Assistant",
        avatar: { type: "image", value: "../../avatars/agents/general.png" },
        badge: { text: "AI Agent", type: "primary" }
      },
      content: "Based on your requirements, I recommend using our async request handling pattern with Redis caching for the high-traffic endpoints.",
      timestamp: "Thursday 2:03 PM"
    },
    {
      id: 6,
      user: {
        name: "Client's Architect AI",
        avatar: { type: "image", value: "../../avatars/agents/code.png" },
        badge: { text: "External AI", type: "external" }
      },
      content: "That aligns perfectly with our Kubernetes deployment. We can auto-scale those services based on queue depth.",
      timestamp: "Thursday 2:03 PM"
    },
    {
      id: 7,
      user: {
        name: "Client",
        avatar: { type: "initial", value: "C", color: "#e91e63" }
      },
      content: "This is amazing! Two AI agents from different companies working together seamlessly! 🚀",
      timestamp: "Thursday 2:04 PM"
    }
  ]
};

// Conversation 3: Multi-agent collaboration
const multiAgentTeam = {
  title: "Multi-Agent Team Collaboration",
  messages: [
    {
      id: 1,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "@research @analyst @writer Create a competitive analysis report",
      timestamp: "Friday 9:00 AM",
      mentions: ["research", "analyst", "writer"]
    },
    {
      id: 2,
      user: {
        name: "Research Agent",
        avatar: { type: "image", value: "../../avatars/agents/research.png" },
        badge: { text: "Research AI", type: "primary" }
      },
      content: "I'll gather data on our top 5 competitors, including pricing, features, and recent product launches...",
      timestamp: "Friday 9:01 AM"
    },
    {
      id: 3,
      user: {
        name: "Analyst Agent",
        avatar: { type: "image", value: "../../avatars/agents/analyst.png" },
        badge: { text: "Analyst AI", type: "primary" }
      },
      content: "I'll identify strategic patterns and opportunities. Looking for gaps in their offerings and potential differentiation points...",
      timestamp: "Friday 9:01 AM"
    },
    {
      id: 4,
      user: {
        name: "Writer Agent",
        avatar: { type: "image", value: "../../avatars/agents/summary.png" },
        badge: { text: "Writer AI", type: "primary" }
      },
      content: "I'll compile everything into an executive summary with actionable recommendations. Will have the draft ready in 30 minutes.",
      timestamp: "Friday 9:02 AM"
    },
    {
      id: 5,
      user: {
        name: "Research Agent",
        avatar: { type: "image", value: "../../avatars/agents/research.png" },
        badge: { text: "Research AI", type: "primary" }
      },
      content: "Research complete. Key findings: Competitor A raised prices 15%, Competitor B launched AI features, Competitor C expanded to Europe.",
      timestamp: "Friday 9:15 AM"
    },
    {
      id: 6,
      user: {
        name: "Analyst Agent",
        avatar: { type: "image", value: "../../avatars/agents/analyst.png" },
        badge: { text: "Analyst AI", type: "primary" }
      },
      content: "Analysis shows opportunity: We're the only solution offering cross-organization AI collaboration. This is our unique differentiator.",
      timestamp: "Friday 9:20 AM"
    },
    {
      id: 7,
      user: {
        name: "Writer Agent",
        avatar: { type: "image", value: "../../avatars/agents/summary.png" },
        badge: { text: "Writer AI", type: "primary" }
      },
      content: "Executive summary ready! Highlighting our unique position as the only platform enabling AI agent federation across organizations.",
      timestamp: "Friday 9:30 AM"
    }
  ],
  typing: {
    user: {
      name: "You",
      avatar: { type: "initial", value: "Y", color: "#4caf50" }
    }
  }
};

// Generate all mockups
async function generateReadmeMockups() {
  console.log('📸 Generating README conversation mockups...\n');

  // Generate cross-platform memory mockup
  await generateMockupFromJSON(crossPlatformConvo, 'readme-cross-platform');

  // Generate cross-org collaboration mockup
  await generateMockupFromJSON(crossOrgCollab, 'readme-cross-org');

  // Generate multi-agent team mockup
  await generateMockupFromJSON(multiAgentTeam, 'readme-multi-agent');

  console.log('\n✨ All README mockups generated!');
}

generateReadmeMockups();
