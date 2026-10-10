FROM agentbc-win-cleanup:20261006
USER root
COPY executors/ /usr/local/lib/python3.11/site-packages/agent_bridge_connect/executors/
COPY executors/ /probe/sdk-amd64/lib/python3.11/site-packages/agent_bridge_connect/executors/
COPY reports.py /usr/local/lib/python3.11/site-packages/agent_bridge_connect/
COPY reports.py /probe/sdk-amd64/lib/python3.11/site-packages/agent_bridge_connect/
USER 1000:1000
