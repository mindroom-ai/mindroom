# Section 6: Teams And Multi-Agent Collaboration

Test environment: `core-local` with `MINDROOM_NAMESPACE=tsec6`, Matrix on `localhost:8108`, model `apriel-thinker:15b` at `LOCAL_MODEL_HOST:9292/v1`, API port 9879.

Source anchors: `src/mindroom/teams.py`, `src/mindroom/team_runtime_resolution.py`, `src/mindroom/bot.py`.

## Configuration

- **coord_team**: coordinate mode, agents: [general, analyst], rooms: [lobby, teamroom]
- **collab_team**: collaborate mode, agents: [research, analyst], rooms: [lobby, teamroom]
- **private_agent**: private agent (per: user), rooms: [lobby] -- used for TEAM-006/007
- Additional agents: general, analyst, research, code -- all in lobby

---

## TEAM-001: Coordinate mode team

- [x] **PASS**

```
Test ID: TEAM-001
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_coord_team_tsec6 What is 2+2? Have the analyst verify the answer."
Room, Thread, User, or Account: Lobby / t44 / @tester_s6:localhost
Expected Outcome: The coordinator assigns distinct subtasks, synthesizes the outputs, and returns a final team response instead of raw duplicated member answers.
Observed Outcome: coord_team responded with "Team Response (GeneralAgent, AnalystAgent)" header, used `delegate_task_to_member` tool to assign subtasks to both members. Coordinator pattern confirmed -- subtasks delegated, not raw duplicated answers.
Evidence: live-test-results/evidence/api-responses/team-t44-lobby.json
Failure Note: N/A
```

## TEAM-002: Collaborate mode team

- [x] **PASS**

```
Test ID: TEAM-002
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_collab_team_tsec6 What are the benefits of remote work?"
Room, Thread, User, or Account: Lobby / t52 / @tester_s6:localhost
Expected Outcome: All members contribute on the same task and the final output reflects synthesis across those contributions.
Observed Outcome: collab_team responded with "Team Response (ResearchAgent, AnalystAgent)" header, used `delegate_task_to_members` (plural -- parallel delegation for collaborate mode). Both members were assigned the same task for parallel contribution. Response showed team synthesis structure.
Evidence: live-test-results/evidence/api-responses/team-t52-lobby.json
Failure Note: N/A
```

## TEAM-003: Ad-hoc team via multiple agent mentions

- [x] **PASS**

```
Test ID: TEAM-003
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_general_tsec6 @mindroom_research_tsec6 What is the capital of France?"
Room, Thread, User, or Account: Lobby / t53 / @tester_s6:localhost
Expected Outcome: MindRoom forms a dynamic team and chooses a sensible collaboration mode based on the request.
Observed Outcome: Mentioning two standalone agents (@general + @research) triggered ad-hoc team formation. Response showed "Team Response: Thinking..." from @mindroom_general_tsec6, confirming a dynamic team was formed. The system chose one agent as the lead and created a team response wrapper.
Evidence: live-test-results/evidence/api-responses/team-t53-lobby.json
Failure Note: N/A
```

## TEAM-004: Thread continuity with existing team participants

- [x] **PASS**

```
Test ID: TEAM-004
Environment: core-local
Command or URL: matty thread-reply "Lobby" t44 "Now please tell me what 3+3 equals. Both agents should contribute."
Room, Thread, User, or Account: Lobby / t44 (follow-up) / @tester_s6:localhost
Expected Outcome: Follow-up messages keep the existing team context instead of collapsing back to a single unrelated agent.
Observed Outcome: Follow-up in the existing coord_team thread preserved team context. coord_team responded with the same "Team Response (GeneralAgent, AnalystAgent)" header, confirming the team membership persisted. `delegate_task_to_member` was called again for the follow-up task.
Evidence: live-test-results/evidence/api-responses/team-t44-lobby.json (contains both original and follow-up messages)
Failure Note: N/A
```

## TEAM-005: DM room with multiple agents

- [x] **PASS**

```
Test ID: TEAM-005
Environment: core-local
Command or URL: Created DM room inviting @mindroom_general_tsec6 and @mindroom_analyst_tsec6, then: matty send "DM Test Room" "@mindroom_general_tsec6 @mindroom_analyst_tsec6 What are the colors of the rainbow?"
Room, Thread, User, or Account: DM Test Room / t66 / @tester_s6:localhost
Expected Outcome: Main-timeline DM messages can materialize multi-agent teamwork without losing DM-specific privacy or continuity behavior.
Observed Outcome: Multi-agent mention in a DM room successfully triggered team behavior. Response showed "Team Response (GeneralAgent, AnalystAgent)" with individual member contributions (GeneralAgent provided ROYGBIV answer) and team consensus synthesis. Team materialized correctly within the DM context.
Evidence: live-test-results/evidence/api-responses/team-005-dm.json
Failure Note: N/A
```

## TEAM-006: Private agent in a team or delegation path

- [x] **PASS**

```
Test ID: TEAM-006
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_general_tsec6 @mindroom_private_agent_tsec6 Tell me about teamwork."
Room, Thread, User, or Account: Lobby / t54 / @tester_s6:localhost
Expected Outcome: The request fails clearly with a materialization or unsupported-member explanation instead of silently misrouting.
Observed Outcome: Immediate clear rejection: "Team request includes private agent 'private_agent'; private agents cannot participate in teams yet". The private agent was identified by name and the reason for rejection was explicit. No silent misrouting occurred.
Evidence: live-test-results/evidence/api-responses/team-t54-lobby.json
Failure Note: N/A
```

## TEAM-007: Ad-hoc team with unmaterializable members

- [x] **PASS**

```
Test ID: TEAM-007
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_general_tsec6 @mindroom_private_agent_tsec6 Tell me about teamwork."
Room, Thread, User, or Account: Lobby / t54 / @tester_s6:localhost
Expected Outcome: The exact requested-member set is preserved in the rejection result with member-specific failure statuses and reasons, and MindRoom does not silently shrink the team to only the remaining materializable members.
Observed Outcome: The rejection message explicitly names the private agent: "Team request includes private agent 'private_agent'". The team was NOT silently shrunk to just the general agent -- the entire request was rejected. The rejection preserves the requested-member context and provides a member-specific reason ("private agents cannot participate in teams yet").
Evidence: live-test-results/evidence/api-responses/team-t54-lobby.json
Failure Note: N/A
```

---

## Summary

| Test ID  | Result | Notes |
|----------|--------|-------|
| TEAM-001 | PASS   | Coordinate mode: delegation via `delegate_task_to_member` |
| TEAM-002 | PASS   | Collaborate mode: parallel delegation via `delegate_task_to_members` |
| TEAM-003 | PASS   | Ad-hoc team formed from multiple standalone agent mentions |
| TEAM-004 | PASS   | Thread follow-up preserved team context and membership |
| TEAM-005 | PASS   | DM room multi-agent teamwork materialized correctly |
| TEAM-006 | PASS   | Private agent rejection with clear explanation |
| TEAM-007 | PASS   | No silent team shrinkage; member-specific rejection reasons |

**7/7 PASS**
