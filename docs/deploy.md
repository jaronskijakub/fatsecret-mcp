# Deploy

The server runs as a container. CI publishes the image `ghcr.io/jaronskijakub/fatsecret-mcp` for Linux amd64 and arm64, tagged `latest` and with the full commit SHA of each push to `main`. Each release also tags its image with the version number, such as `0.1.0`.

Before deploying, prepare the configuration and add everyone with `add-user` ([setup](setup.md)). Every person needs a connector password for HTTP.

## Run exactly one instance

Confirmed drafts are remembered in memory only. If two instances serve at once, or a new one starts while a draft is still open, confirming that draft again would write its entries a second time. So:

- run one replica, never more;
- make sure a deploy stops the old instance once the new one is healthy, without a long overlap;
- don't put the server behind anything that sleeps and wakes it per request. A sleeping server also forgets its caches and request counts.

## Railway

These steps run the published image on [Railway](https://railway.com). An always-on service with one replica uses little memory and CPU, but it isn't free: check your plan's included usage, and look at the actual usage after a few days.

1. **Create the service.** In a new project, choose to deploy a Docker image and enter the image with a fixed tag: a release version such as `ghcr.io/jaronskijakub/fatsecret-mcp:0.1.0`, or a full commit SHA. A fixed tag means a new version deploys only when you change it.
2. **Settings.** Set the healthcheck path to `/health`. Keep Serverless off and replicas at 1. Don't set `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS`: by default Railway removes the old deployment once the new one is active.
3. **Networking.** Generate a Railway domain that targets port `8080`.
4. **Variables.**
   - `PORT` = `8080`
   - `FATSECRET_CONSUMER_KEY`, `FATSECRET_CONSUMER_SECRET` and `FATSECRET_MCP_SIGNING_KEY` from your `.env`
   - `FATSECRET_MCP_BASE_URL` = `https://<your-domain>.up.railway.app`, with no path or trailing slash
   - `FATSECRET_MCP_USERS`, the JSON line from `add-user`
   - optionally `FATSECRET_MCP_CLIENTS` = `claude,chatgpt`, to let ChatGPT sign in as well

   The Raw Editor suits plain `KEY=value` lines. Add the JSON value through New Variable instead, pasted without the surrounding single quotes.
5. **Deploy and check.** Wait for the deployment to turn active, then open `https://<your-domain>.up.railway.app/health`. It returns `{"status":"ok"}`.
6. **Spending limit.** In your workspace's usage settings, set a Compute email alert, and a hard limit if you want one. A hard limit stops every service in the workspace, not only this one.
7. **Add the connector** in Claude or ChatGPT ([connector](connector.md)).

To update, change the image tag to a newer version or commit SHA and redeploy. When a release changes the tools, its changelog entry says so: reconnect the connector in Claude or ChatGPT afterwards, since the app keeps the tool descriptions it saw when it connected. To add a person, run `add-user` against your current `FATSECRET_MCP_USERS` and replace the variable with the new value. Variables survive deploys.

## Other container hosts

Any host that runs a public container image and gives it an HTTPS address will do. What the server needs:

- the variables listed under Railway step 4, supplied at runtime;
- traffic to the port in `PORT` (8080 by default), with HTTPS terminated in front of it;
- one always-on instance, as described above;
- request logs turned off or kept private: sign-in URLs carry one-time codes.

FatSecret calls are signed with OAuth 1.0, so the server works from changing outbound IP addresses. If `check` or the logs report error 21, look at the IP restrictions in your FatSecret developer console.

## Run with Docker

Use the same configuration as above. Docker's `--env-file` expects `KEY=value` lines without shell quotes, so keep `FATSECRET_MCP_USERS` as a single JSON line without the surrounding single quotes.

```sh
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 \
  -e FATSECRET_MCP_BASE_URL=http://127.0.0.1:8080 \
  ghcr.io/jaronskijakub/fatsecret-mcp:latest
```

In a second terminal:

```sh
curl --fail http://127.0.0.1:8080/health
```

Expect `{"status":"ok"}`; retry if the server is still starting. Stop it with Ctrl+C in the first terminal.

The image starts HTTP on `0.0.0.0:8080`, runs as a non-root user, and checks `/health` itself. Set `PORT` to change the listening port and adjust the published port to match.

## Without a container

```sh
uvx --env-file .env fatsecret-mcp serve --transport http --host 127.0.0.1 --port 8000
```

Put an HTTPS reverse proxy in front of it and set `FATSECRET_MCP_BASE_URL` to the public origin. Configuration errors stop the server before it starts listening.

## Build and test the image

In a fork, CI publishes `ghcr.io/<owner>/<repository>` instead. The first publication creates a private GHCR package: change its visibility to Public in the package settings, then rerun CI. The last CI step checks that the image can be pulled without signing in.

To build and test locally, with Docker running:

```sh
docker build -t fatsecret-mcp:local .
FATSECRET_CONTAINER_IMAGE=fatsecret-mcp:local uv run pytest tests/test_container.py -v -s
```

The container tests use synthetic credentials, make no FatSecret calls, and report the time from starting the container to a healthy HTTP response. The final image contains the installed package without build or development tools.
