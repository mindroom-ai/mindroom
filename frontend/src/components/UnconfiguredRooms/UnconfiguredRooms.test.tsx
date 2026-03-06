import { render, screen } from '@testing-library/react';
import { describe, expect, it, type Mock } from 'vitest';
import { UnconfiguredRooms } from './UnconfiguredRooms';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('UnconfiguredRooms', () => {
  it('renders teams in the external room list', async () => {
    (global.fetch as Mock).mockResolvedValueOnce(
      jsonResponse({
        agents: [
          {
            agent_id: 'test_team',
            display_name: 'Test Team',
            configured_rooms: ['team_room'],
            joined_rooms: ['team_room', '!external_room:localhost'],
            unconfigured_rooms: ['!external_room:localhost'],
            unconfigured_room_details: [
              { room_id: '!external_room:localhost', name: 'Partner Room' },
            ],
          },
        ],
      })
    );

    render(<UnconfiguredRooms />);

    expect(await screen.findByText('Test Team')).toBeInTheDocument();
    expect(screen.getByText('Partner Room')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Manage rooms that agents and teams have joined but are not in the configuration'
      )
    ).toBeInTheDocument();
    expect(screen.getByText(/1 external room found across 1 entity/i)).toBeInTheDocument();
  });
});
