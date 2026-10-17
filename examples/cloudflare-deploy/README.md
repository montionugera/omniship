# Cloudflare deployment

This pipeline checks the application's types, builds a static site into
`dist`, and publishes that artifact to Cloudflare Pages. Copy `workflow.py`
into your application and adjust the scripts, Pages project name, and pinned
toolchain versions.

Configure these GitHub Actions secrets:

- `CLOUDFLARE_API_TOKEN`: an API token allowed to edit Pages or Workers.
- `CLOUDFLARE_ACCOUNT_ID`: the owning account ID.

The account ID can alternatively be supplied through `account_id` on either
block. Authentication always comes from the execution environment.
The example declares a GitHub deployment environment named `production`.

Run `omniship generate` and commit the generated files. The default Check
workflow runs on PRs/main pushes, while Ship is triggered by `v*` tags and calls
Build, which calls Check. Preview deployment of fork PRs needs a separate
credentials/access policy; this example does not grant secrets to fork code.

## Ownership

The plugin owns Cloudflare publication only. Framework builds stay in Node,
Bun, or custom tasks, which register the deployable directory as an artifact
(here `NodeBuild(artifacts=("dist",))` registers `dist`). Ship copies exactly
one artifact with the selected name into a temporary directory outside the
workspace and deploys that copy, so a deployment never reads, bundles, or
rebuilds workspace source. Artifacts must be directories without symlinks.

## Pages

`CloudflarePagesDeploy(project=..., branch=...)` runs `wrangler pages deploy`
for the artifact. Cloudflare decides the environment from the branch: the
project's production branch publishes to production and any other branch
creates a preview. The branch is required so CI never relies on Git detection.
Pages Functions in a workspace `functions/` directory are not compiled; ship
them as an already-built `_worker.js` inside the artifact.

## Workers

`CloudflareWorkerDeploy` publishes a prebuilt Worker described by a Wrangler
configuration file inside the artifact (`config`, default `wrangler.json`;
`.jsonc` and `.toml` also work). The configuration's `main` and assets paths
must point to files inside the artifact, such as the build output of the
Cloudflare Vite plugin, which writes a deployable configuration next to the
built Worker. Wrangler bundling is disabled.

```python
from omniship.plugins.cloudflare import CloudflareWorkerDeploy

stage.task(
    CloudflareWorkerDeploy(
        config="api/wrangler.json",
        environment="staging",  # Wrangler environment, optional
        worker="api-staging",  # overrides the configured Worker name, optional
        target="production",  # or "preview" (default)
    ),
    requires=[node, wrangler],
    execution=github.job(env=env, outputs=["deployment_url"]),
)
```

`target="preview"` runs `wrangler versions upload`, creating a version with a
preview URL that serves no production traffic; `preview_alias="pr-12"` adds a
stable alias. `target="production"` runs `wrangler deploy`.

## Deployment URL

Deployment sets the `deployment_url` task output when Cloudflare reports one:
the Pages deployment URL, the Worker version's preview (alias) URL, or the
production `workers.dev` URL. A production Worker served only on routes or
custom domains has no reported URL, so the output is not set. Declare
`github.job(outputs=["deployment_url"])` to expose it to dependent GitHub jobs.

OmniShip reads the URL from Wrangler's machine-readable output file
(`WRANGLER_OUTPUT_FILE_PATH`) rather than from log text. CLI output streams
through built-in task logging with the API token redacted. The token is never
added to command arguments or serialized config.

The imperative equivalent inside a decorated Ship task is:

```python
from omniship.plugins.cloudflare import Cloudflare

url = Cloudflare(ctx).deploy_pages(project="my-site", branch="main")
Cloudflare(ctx).deploy_worker(target="production", config="api/wrangler.json")
```

The example is tested against Wrangler **4.145.0**. Use a version supporting
`versions upload --preview-alias`, `deploy --no-bundle`, and the
`WRANGLER_OUTPUT_FILE_PATH` output file.

References: [Pages direct upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[Wrangler commands](https://developers.cloudflare.com/workers/wrangler/commands/),
[Workers versions](https://developers.cloudflare.com/workers/configuration/versions-and-deployments/).
