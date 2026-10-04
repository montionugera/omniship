"""Build Vercel output in CI, then publish the transferred artifact."""

from omniship import Pipeline
from omniship.plugins.github import GitHubActions
from omniship.plugins.node import NodeTest, NodeToolchain
from omniship.plugins.vercel import VercelBuild, VercelDeploy, VercelToolchain

github = GitHubActions()
pipeline = Pipeline(targets=[github])
node = NodeToolchain(version="24.8.0")
vercel = VercelToolchain(version="62.2.0")
env = {
    name: github.secret(name)
    for name in (
        "VERCEL_TOKEN",
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
    )
}


@pipeline.check
def check(stage):
    stage.task(NodeTest(script="typecheck"), requires=[node])


@pipeline.build
def build(stage):
    stage.task(
        VercelBuild(target="production"),
        requires=[node, vercel],
        execution=github.job(env=env),
    )


@pipeline.ship
def ship(stage):
    stage.task(
        VercelDeploy(target="production"),
        requires=[node, vercel],
        execution=github.job(
            env=env, environment="production", outputs=["deployment_url"]
        ),
    )
