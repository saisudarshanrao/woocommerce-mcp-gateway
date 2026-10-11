-- A server the platform runs itself. An admin gives a container image instead
-- of a URL; the provisioner deploys it into a namespace of its own and the
-- gateway talks to it like any other server. So a hosted server is an ordinary
-- upstream_servers row, with its URL and credential filled in by the gateway,
-- plus what to run and how the deployment is going.
ALTER TABLE upstream_servers
    -- NULL for a server registered by URL. Otherwise what to run, as JSON:
    -- image, arguments, environment, port, paths, whether it may reach the internet.
    ADD COLUMN hosted            text,
    --   deploying  being started, or restarted after an edit
    --   running    its pod is up. Whether it answers MCP is the usual health check.
    --   failed     gave up; hosted_detail says why. Can be tried again or removed.
    --   deleting   being removed. The row goes when its namespace is gone.
    ADD COLUMN hosted_status     text CHECK (hosted_status IN ('deploying', 'running', 'failed', 'deleting')),
    ADD COLUMN hosted_detail     text,
    -- When the current attempt began; the deployment timeout counts from here.
    ADD COLUMN hosted_started_at timestamptz;
