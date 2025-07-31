# Test Plan: Prevent Duplicate Responses After Restart

## Objective
Verify that agents don't respond to old messages when the bot is restarted.

## Prerequisites
1. Matrix server running (dendrite)
2. mindroom_user account created
3. Test room(s) created with agents invited

## Test Steps

### Step 1: Initial Run
1. Start the bot: `mindroom run`
2. In your Matrix client, send a test message mentioning an agent (e.g., "@mindroom_calculator:localhost what is 2+2?")
3. Verify the agent responds
4. Note the exact response and timestamp

### Step 2: Stop and Restart
1. Stop the bot (Ctrl+C)
2. Wait a few seconds
3. Check that sync token stores were created:
   ```bash
   ls -la tmp/stores/
   ```
   You should see directories for each agent.

### Step 3: Verify No Duplicates
1. Start the bot again: `mindroom run`
2. Observe the logs - agents should NOT respond to the old message again
3. The old message should not trigger any "WILL PROCESS" log entries

### Step 4: Verify New Messages Work
1. Send a NEW message mentioning an agent
2. Verify the agent responds normally to the new message

## Expected Results
- ✅ Agents respond to messages in the first run
- ✅ Sync token stores are created in `tmp/stores/{agent_name}/`
- ✅ Agents do NOT respond to old messages after restart
- ✅ Agents DO respond to new messages after restart

## How to Verify the Fix
Look for these indicators:

### Before Fix (Bad Behavior)
- After restart, you see: `[agent_name] WILL PROCESS message from...` for OLD messages
- Agents send duplicate responses to the same messages

### After Fix (Good Behavior)
- After restart, NO "WILL PROCESS" logs for old messages
- Only new messages trigger responses
- Sync token files exist in `tmp/stores/`

## Debugging
If agents still respond to old messages:
1. Check if `tmp/stores/{agent_name}/` directories exist
2. Check if there are files inside those directories
3. Look for any errors about "store_path" in the logs
4. Ensure you're using the fix/prevent-duplicate-responses branch
