FROM agentbc-win-claude:20261006
# Build driver verifies base image ID before building.
USER root
RUN rm -f /opt/agentbc/bin/codex
COPY codex /opt/agentbc/bin/codex
COPY session_cleanup.py execution_policy.py container_session_cleanup.py /usr/local/lib/python3.11/site-packages/agent_bridge_connect/
COPY session_cleanup.py execution_policy.py container_session_cleanup.py /probe/sdk-amd64/lib/python3.11/site-packages/agent_bridge_connect/
RUN chmod 755 /opt/agentbc/bin/codex && test ! -L /opt/agentbc/bin/codex
USER 1000:1000
