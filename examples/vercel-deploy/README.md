# Vercel deployment

This pipeline checks the application's types, builds a Vercel Build Output API
artifact, and deploys that artifact to production. Copy `workflow.py` into your
application and adjust the check script and pinned toolchain versions.

Configure these GitHub Actions secrets:

- `VERCEL_TOKEN`: a Vercel access token.
- `VERCEL_ORG_ID`: the owning team/account ID.
- `VERCEL_PROJECT_ID`: the existing project's ID.

The IDs can alternatively be supplied through `project_id` and `team_id` on
both blocks. Authentication always comes from the execution environment.
The example declares a GitHub deployment environment named `production`.

Run `omniship generate` and commit the generated files. The default Check
workflow runs on PRs/main pushes, while Ship is triggered by `v*` tags and calls
Build, which calls Check. Configure the GitHub workflow triggers if you want
deployment on pushes instead. Preview deployment of fork PRs needs a separate
credentials/access policy; this example does not grant secrets to fork code.

`VercelBuild` runs `vercel pull` for the selected environment and
`vercel build --standalone`, exporting fresh output under `.omniship`. Only the
output directory is registered as the `output` artifact; `.vercel` environment
cache files are not bundled. Build-time environment values may still be
embedded by the framework in compiled output, so use the framework's usual
public/private environment-variable rules.

`VercelDeploy` consumes exactly one artifact named `output`, stages it as
`.vercel/output` in a temporary directory, and runs `vercel deploy --prebuilt`.
It does not pull settings or rebuild source. Artifact metadata rejects a
different project/team/target when the artifact came from `VercelBuild`.
Use the same `target` in both stages: `preview` (default), `production`, or a
configured Vercel custom environment such as `staging`.

Framework/language builds remain usable: register a valid `.vercel/output`
directory with `ctx.artifacts.add(".vercel/output")` or the existing build block's
artifact option, then use `VercelDeploy`. An arbitrary `dist` directory is not
a Vercel Build Output API artifact.

The imperative equivalent inside decorated tasks is:

```python
from omniship.plugins.vercel import Vercel

# In a Build task, after provisioning Node and the Vercel CLI:
artifact = Vercel(ctx).build(target="production")

# In a separate Ship task with imported artifacts:
url = Vercel(ctx).deploy(target="production")
ctx.log.info(f"Published {url}")
```

Deployment sets the `deployment_url` task output. Declare
`github.job(outputs=["deployment_url"])` to expose it to dependent GitHub jobs.
CLI output streams through built-in task logging with the access token
redacted. The token is never added to command arguments or serialized config.

The example is tested against Vercel CLI **62.2.0**. Use a version supporting
environment-token authentication, custom targets, `build --output`, and
`build --standalone`. Output must use Build Output API version 3 and contain no
symlinks, matching OmniShip's portable artifact contract. Prebuilt deployments
have Vercel's documented limitations around system build-time variables and
Next.js Skew Protection; configure the framework accordingly.

References: [Vercel build](https://vercel.com/docs/cli/build),
[prebuilt deployment](https://vercel.com/docs/cli/deploy),
[custom CI workflows](https://vercel.com/kb/guide/using-vercel-cli-for-custom-workflows).
