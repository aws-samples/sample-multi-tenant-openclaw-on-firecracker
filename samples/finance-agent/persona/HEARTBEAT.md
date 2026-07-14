# Keep this file empty (or with only comments) to skip heartbeat API calls.

# Add tasks below when you want the agent to check something periodically.

<!--
Heartbeat contract (see AGENTS.md §Heartbeats):
  For heartbeat polls, read HEARTBEAT.md if it exists. If there is no explicit
  configured task, reply HEARTBEAT_OK.

How to register a periodic task (cron):
  openclaw cron add --name '<name>' --every 5m \
    --message '<msg>' --channel jarvis --to '<channelId>:thread:<threadId>'

Example task lines (uncomment and edit):
# CHECK the weather for <city> every morning -> message the user
-->
