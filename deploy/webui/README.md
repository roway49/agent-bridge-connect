# WebUI container integration (acceptance pending)

Merge `compose.yaml` into the Docker mainline Compose using `-f`. It deliberately
requires the mainline image digest; it does not create a second AgentBC image or
certify that an untested image supports the service. The image must contain the
packaged static assets and `agentbc webui serve` options.

Set `AGENTBC_IMAGE`, `AGENTBC_WEBUI_PORT` to the saved fixed host port,
`AGENTBC_RECORD_ROOT` and `AGENTBC_REPORT_ROOT` to existing readable directories.
Set UID/GID to match the read-only data. Never mount executor credentials or the
full host AgentBC configuration into this service. Linux ownership and Windows
Docker bind permissions must be verified on their respective hosts.

With the same `-f` files and project name on every invocation:

- N (default): `AGENTBC_WEBUI_RESTART=no`; manually `docker compose --profile webui up -d webui`.
- y: `AGENTBC_WEBUI_RESTART=unless-stopped`; start once and enable the host Docker service/Desktop login startup. Container restart cannot start a stopped Docker daemon.
- Close: `docker compose stop webui`. Reopen with `docker compose start webui`.
- Retrieve the private browser link with `docker compose exec webui cat /run/webui/launch-url`; do not publish or retain it in reports.
- Native `agentbc webui close` controls only its native configuration instance.

The container uses port 8080 internally and exact loopback host-origin validation;
only the configured host port is published, on 127.0.0.1. No wildcard origins.
A tmpfs holds the private launch link; each start replaces its contents only after
checking ownership, permissions and file type. Recreation clears it.

Required manual matrix: macOS arm64, Linux amd64, Windows Desktop/WSL2 amd64.
For each record image digest, host, start/stop/restart, fixed-port conflict,
a real task/revision text comparison, category transition with retained detail,
report opening, disconnect recovery and unchanged data. Each untested cell remains
pending. This matrix does not replace executor dispatch/execution acceptance.
