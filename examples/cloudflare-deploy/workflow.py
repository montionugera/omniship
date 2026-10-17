"""Build a static site in CI, then publish the transferred artifact to Pages."""

from omniship import Pipeline
from omniship.plugins.cloudflare import CloudflarePagesDeploy, CloudflareToolchain
from omniship.plugins.github import GitHubActions
from omniship.plugins.node import NodeBuild, NodeTest, NodeToolchain

github = GitHubActions()
pipeline = Pipeline(targets=[github])
node = NodeToolchain(version="24.8.0")
wrangler = CloudflareToolchain(version="4.145.0")
env = {
    name: github.secret(name)
    for name in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    )
}


@pipeline.check
def check(stage):
    stage.task(NodeTest(script="typecheck"), requires=[node])


@pipeline.build
def build(stage):
    stage.task(NodeBuild(artifacts=("dist",)), requires=[node])


@pipeline.ship
def ship(stage):
    stage.task(
        CloudflarePagesDeploy(project="my-site", branch="main"),
        requires=[node, wrangler],
        execution=github.job(
            env=env, environment="production", outputs=["deployment_url"]
        ),
    )
