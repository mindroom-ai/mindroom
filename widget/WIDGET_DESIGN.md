# MindRoom Widget: API Integration Hub

## Executive Summary

The MindRoom Widget is a Matrix-native visual interface that extends MindRoom's capabilities by providing seamless integration with external services. It acts as a bridge between users, MindRoom agents, and third-party APIs, enabling agents to interact with services like Gmail, Amazon, Netflix, GitHub, and more through a secure, user-friendly interface.

## Vision & Goals

### Primary Objectives
1. **Unified API Management**: Single interface to connect and manage all external service integrations
2. **Secure Authentication**: Handle OAuth flows and API keys with military-grade security
3. **Agent Empowerment**: Enable MindRoom agents to access external services on behalf of users
4. **Visual Workflows**: Create visual tools for building multi-service workflows
5. **Privacy First**: Maintain MindRoom's privacy principles with granular permission controls

### Key Benefits
- **No Code Required**: Visual interface for non-technical users
- **Agent Superpowers**: Agents can book flights, check emails, manage calendars, etc.
- **Workflow Automation**: Chain multiple services together for complex tasks
- **Audit Trail**: Complete visibility into what agents are doing with your accounts
- **Room-Based Permissions**: Different API access for different Matrix rooms

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          Matrix Client                            │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    MindRoom Widget                       │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │    │
│  │  │   Service   │  │     Auth     │  │   Activity   │  │    │
│  │  │    Hub      │  │   Manager    │  │   Monitor    │  │    │
│  │  └─────────────┘  └──────────────┘  └──────────────┘  │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │    │
│  │  │  Workflow   │  │   Command    │  │   Settings   │  │    │
│  │  │   Builder   │  │   Builder    │  │   Manager    │  │    │
│  │  └─────────────┘  └──────────────┘  └──────────────┘  │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 │ Matrix Protocol
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                        MindRoom Agents                           │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   General   │  │   Research   │  │   Service-Specific   │  │
│  │    Agent    │  │    Agent     │  │      Agents          │  │
│  └─────────────┘  └──────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 │ Secure API Gateway
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      External Services                           │
│  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────┐  ┌────────────┐  │
│  │Gmail │  │ Amazon │  │Netflix │  │GitHub│  │   Others    │  │
│  └──────┘  └────────┘  └────────┘  └──────┘  └────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Service Integration Hub
**Purpose**: Central dashboard for managing all service connections

**Features**:
- Service catalog with 50+ pre-built integrations
- Connection status dashboard
- Quick actions for common tasks
- Service health monitoring
- Usage statistics and rate limit tracking

**User Story**: "I want to see all my connected services at a glance and quickly perform common actions"

### 2. Authentication Manager
**Purpose**: Secure handling of all authentication flows

**Features**:
- OAuth 2.0/PKCE implementation
- API key management with encryption
- Token refresh automation
- Multi-account support per service
- Permission scoping and review

**User Story**: "I want to connect my Gmail without giving MindRoom my password"

### 3. Workflow Builder
**Purpose**: Visual tool for creating multi-step, multi-service workflows

**Features**:
- Drag-and-drop workflow designer
- Pre-built workflow templates
- Conditional logic and branching
- Schedule-based triggers
- Testing and debugging tools

**User Story**: "When I get an important email, I want it to create a calendar event and notify me on Slack"

### 4. Command Builder
**Purpose**: Visual interface for building complex agent commands

**Features**:
- Natural language command preview
- Parameter configuration
- Service action browser
- Command history and favorites
- Batch operations support

**User Story**: "I want to tell my agent to 'check my Gmail for invoices and add them to my expense spreadsheet'"

### 5. Activity Monitor
**Purpose**: Real-time visibility into all API activities

**Features**:
- Live activity feed
- Detailed audit logs
- Performance metrics
- Error tracking and debugging
- Cost estimation (for paid APIs)

**User Story**: "I want to see exactly what my agents are doing with my connected accounts"

### 6. Settings Manager
**Purpose**: Granular control over permissions and behaviors

**Features**:
- Room-based permission settings
- Agent-specific access controls
- Rate limit configuration
- Data retention policies
- Privacy controls

**User Story**: "I want my work room to access Gmail but not my personal shopping accounts"

## Service Integration Examples

### Gmail Integration
```yaml
capabilities:
  - read_emails: Search and read emails
  - send_emails: Compose and send emails
  - manage_labels: Create and apply labels
  - manage_filters: Create email filters
  - attachments: Upload/download attachments

example_commands:
  - "Find all unread emails from my boss"
  - "Send a thank you email to the team"
  - "Archive all promotional emails older than 30 days"
```

### Amazon Integration
```yaml
capabilities:
  - order_history: View past orders
  - track_packages: Real-time tracking
  - wishlists: Manage wishlists
  - product_search: Search products
  - price_tracking: Monitor price changes

example_commands:
  - "What's the status of my recent orders?"
  - "Add this book to my wishlist"
  - "Alert me when this item goes on sale"
```

### Calendar Integration
```yaml
capabilities:
  - view_events: Read calendar events
  - create_events: Schedule new events
  - manage_reminders: Set reminders
  - availability: Check free/busy times
  - meeting_scheduling: Find optimal meeting times

example_commands:
  - "Schedule a meeting with John next week"
  - "What's on my calendar tomorrow?"
  - "Block 2 hours for deep work every morning"
```

## Technical Architecture

### Frontend Stack
- **Framework**: React 18 with TypeScript
- **Build Tool**: Vite (fast HMR, optimized builds)
- **State Management**: Zustand (lightweight, TypeScript-first)
- **UI Library**: Tailwind CSS + shadcn/ui
- **API Client**: TanStack Query
- **Testing**: Vitest + React Testing Library

### Backend Components
- **API Gateway**: FastAPI (Python)
- **Authentication**: OAuth2 client + JWT tokens
- **Storage**: Encrypted credentials in Matrix room state
- **Caching**: Redis for API responses
- **Rate Limiting**: Token bucket algorithm

### Security Architecture
- **Credential Storage**: AES-256 encryption
- **Token Management**: Automatic refresh, secure storage
- **Permission Model**: RBAC with room-based scoping
- **Audit Logging**: Immutable audit trail in Matrix
- **API Isolation**: Separate contexts per room

## User Experience Flow

### Initial Setup
1. User opens widget in Matrix client
2. Widget loads with service catalog
3. User selects service to connect
4. OAuth flow or API key entry
5. Successful connection confirmed
6. Service appears in dashboard

### Daily Usage
1. User types command in Matrix: "@mindroom_agent check my Gmail"
2. Agent recognizes Gmail command
3. Agent queries widget for Gmail access
4. Widget executes API call securely
5. Results returned to agent
6. Agent formats and presents response

### Workflow Creation
1. User opens Workflow Builder
2. Drags "Gmail trigger" onto canvas
3. Configures "New email from boss" condition
4. Adds "Create calendar event" action
5. Saves and activates workflow
6. Workflow runs automatically

## Implementation Roadmap

### Phase 1: Foundation (Weeks 1-4)
- [ ] Basic widget scaffold with Matrix integration
- [ ] Authentication framework
- [ ] Service registry system
- [ ] Gmail integration (pilot service)
- [ ] Basic activity logging

### Phase 2: Core Services (Weeks 5-8)
- [ ] Calendar integration
- [ ] GitHub integration
- [ ] Amazon integration (read-only)
- [ ] Command builder UI
- [ ] Permission system

### Phase 3: Advanced Features (Weeks 9-12)
- [ ] Workflow builder
- [ ] Multi-account support
- [ ] Advanced activity monitoring
- [ ] Service health dashboard
- [ ] Rate limit management

### Phase 4: Scale & Polish (Weeks 13-16)
- [ ] 10+ additional service integrations
- [ ] Workflow templates marketplace
- [ ] Advanced security features
- [ ] Performance optimization
- [ ] Documentation and tutorials

## Success Metrics

### Technical Metrics
- API response time < 500ms
- Widget load time < 2s
- 99.9% uptime for critical services
- Zero credential leaks
- <1% API call failure rate

### User Metrics
- Time to connect first service < 2 minutes
- 80% of users connect 3+ services
- 50% weekly active usage
- <5% disconnect rate
- 90% success rate for agent commands

## Privacy & Security Considerations

### Data Minimization
- Store only essential credentials
- Automatic token expiration
- No caching of sensitive data
- User-controlled data retention

### Access Control
- Granular permissions per service
- Room-based isolation
- Agent-specific restrictions
- Audit trail for all access

### Compliance
- GDPR-compliant data handling
- Right to deletion support
- Data portability features
- Clear privacy policy

## Open Questions & Decisions

1. **Hosting Model**: Self-hosted only or offer hosted option?
2. **Monetization**: Free with premium features or fully open source?
3. **Service Coverage**: Focus on top 10 services or long tail?
4. **Mobile Support**: Progressive web app or native widget?
5. **Offline Support**: Queue commands when services unavailable?

## Conclusion

The MindRoom Widget transforms MindRoom from a conversational AI platform into a true digital assistant that can take actions across all your digital services. By maintaining security, privacy, and user control as core principles, we create a powerful yet trustworthy bridge between AI agents and the external world.

This design prioritizes user experience, security, and extensibility, ensuring the widget can grow with user needs while maintaining the core values of the MindRoom project.
