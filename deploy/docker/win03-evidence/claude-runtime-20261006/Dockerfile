FROM sha256:41c1bdd18c5d0642da63fa5c3616a5834be8551bc4e3254d8ffa6e6cc4662348
USER root
COPY claude /opt/claude/claude
COPY claude_agent_sdk-0.2.142-py3-none-manylinux_2_17_x86_64.whl /build/claude-sdk/
COPY requirements.txt /build/claude-sdk/requirements.txt
RUN python -m venv /probe/sdk-amd64 \
 && /probe/sdk-amd64/bin/pip install --no-index --no-deps /build/claude-sdk/*.whl \
 && /probe/sdk-amd64/bin/pip install --require-hashes -r /build/claude-sdk/requirements.txt \
 && /probe/sdk-amd64/bin/pip check \
 && cp -a /usr/local/lib/python3.11/site-packages/agent_bridge_connect /probe/sdk-amd64/lib/python3.11/site-packages/ \
 && mkdir -p /profiles/claude && chown 1000:1000 /profiles/claude \
 && chmod 755 /opt/claude/claude
COPY runtime_launcher.py /opt/agentbc/bin/claude
RUN chmod 755 /opt/agentbc/bin/claude \
 && printf '#!/bin/sh\nexec /probe/sdk-amd64/bin/python -m agent_bridge_connect.cli "$@"\n' > /usr/local/bin/agentbc \
 && chmod 755 /usr/local/bin/agentbc
ENV PATH=/opt/agentbc/bin:/probe/sdk-amd64/bin:/usr/local/bin:/usr/bin:/bin
USER 1000:1000
