"""
CLI entry point for rds-pg-migration-toolkit.

Provides the following commands:
  - preflight: Run pre-flight checks before migration
  - migrate:   Create and manage blue-green deployments
  - verify:    Verify migration success
  - maintain:  Run post-migration maintenance tasks
  - rollback:  Roll back a failed migration
"""

import click

from rds_migrate import __version__
from rds_migrate.utils import configure_logging, get_rds_client


@click.group()
@click.version_option(version=__version__, prog_name="rds-pg-migration-toolkit")
@click.option("--profile", default="default", help="AWS CLI profile name.")
@click.option("--region", default="us-east-1", help="AWS region.")
@click.option(
    "--output",
    type=click.Choice(["table", "json", "csv"]),
    default="table",
    help="Output format.",
)
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, profile: str, region: str, output: str, verbose: bool) -> None:
    """rds-pg-migration-toolkit: Safe, zero-downtime PostgreSQL upgrades on AWS RDS/Aurora."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile
    ctx.obj["region"] = region
    ctx.obj["output"] = output
    ctx.obj["verbose"] = verbose
    configure_logging(verbose=verbose)


@cli.command()
@click.option("--instance", required=True, help="RDS instance or Aurora cluster identifier.")
@click.option("--target-version", required=True, help="Target PostgreSQL major version (e.g., 16).")
@click.option("--config", default=None, type=click.Path(exists=True), help="Path to YAML config file.")
@click.pass_context
def preflight(ctx: click.Context, instance: str, target_version: str, config: str | None) -> None:
    """Run pre-flight checks before migration.

    Validates parameter group compatibility, extension support, storage space,
    and blue-green deployment eligibility.
    """
    from rds_migrate.preflight import run_preflight_checks

    rds_client = get_rds_client(
        profile=ctx.obj["profile"],
        region=ctx.obj["region"],
    )
    results = run_preflight_checks(
        rds_client=rds_client,
        instance_id=instance,
        target_version=target_version,
        config_path=config,
    )
    # TODO: Format output based on ctx.obj["output"]
    click.echo(results)


@cli.command()
@click.option("--instance", required=True, help="RDS instance or Aurora cluster identifier.")
@click.option("--target-version", required=True, help="Target PostgreSQL major version (e.g., 16).")
@click.option("--wait/--no-wait", default=True, help="Wait for deployment to complete.")
@click.option("--switchover/--no-switchover", default=False, help="Automatically switchover after deployment is ready.")
@click.option("--timeout", default=3600, type=int, help="Timeout in seconds for deployment (default: 3600).")
@click.pass_context
def migrate(
    ctx: click.Context,
    instance: str,
    target_version: str,
    wait: bool,
    switchover: bool,
    timeout: int,
) -> None:
    """Create a blue-green deployment for PostgreSQL major version upgrade.

    Creates a green environment with the target PG version, monitors replication,
    and optionally performs switchover.
    """
    from rds_migrate.migrate import create_blue_green_deployment

    rds_client = get_rds_client(
        profile=ctx.obj["profile"],
        region=ctx.obj["region"],
    )
    result = create_blue_green_deployment(
        rds_client=rds_client,
        instance_id=instance,
        target_version=target_version,
        wait=wait,
        switchover=switchover,
        timeout=timeout,
    )
    click.echo(result)


@cli.command()
@click.option("--instance", required=True, help="RDS instance or Aurora cluster identifier.")
@click.option("--expected-version", required=True, help="Expected PostgreSQL version after migration.")
@click.option("--check-connections", is_flag=True, default=True, help="Verify active connections.")
@click.option("--check-replication", is_flag=True, default=False, help="Check replication status.")
@click.pass_context
def verify(
    ctx: click.Context,
    instance: str,
    expected_version: str,
    check_connections: bool,
    check_replication: bool,
) -> None:
    """Verify migration success after switchover.

    Checks PG version, validates connections, and optionally verifies
    replication status.
    """
    from rds_migrate.verify import run_verification

    rds_client = get_rds_client(
        profile=ctx.obj["profile"],
        region=ctx.obj["region"],
    )
    result = run_verification(
        rds_client=rds_client,
        instance_id=instance,
        expected_version=expected_version,
        check_connections=check_connections,
        check_replication=check_replication,
    )
    click.echo(result)


@cli.command()
@click.option("--instance", required=True, help="RDS instance or Aurora cluster identifier.")
@click.option("--analyze", is_flag=True, default=False, help="Run ANALYZE on all tables.")
@click.option("--reindex", is_flag=True, default=False, help="Run REINDEX on critical indexes.")
@click.option("--check-bloat", is_flag=True, default=False, help="Check for table and index bloat.")
@click.option("--connection-string", default=None, help="PostgreSQL connection string for direct DB access.")
@click.pass_context
def maintain(
    ctx: click.Context,
    instance: str,
    analyze: bool,
    reindex: bool,
    check_bloat: bool,
    connection_string: str | None,
) -> None:
    """Run post-migration maintenance tasks.

    Performs ANALYZE, REINDEX, and bloat detection to restore performance
    after blue-green switchover (cold cache recovery).
    """
    from rds_migrate.maintain import run_maintenance

    result = run_maintenance(
        instance_id=instance,
        run_analyze=analyze,
        run_reindex=reindex,
        check_bloat=check_bloat,
        connection_string=connection_string,
    )
    click.echo(result)


@cli.command()
@click.option("--deployment-id", required=True, help="Blue-green deployment identifier (e.g., bgd-xxxx).")
@click.option("--delete-green", is_flag=True, default=True, help="Delete the green environment.")
@click.pass_context
def rollback(ctx: click.Context, deployment_id: str, delete_green: bool) -> None:
    """Roll back a failed or unwanted blue-green deployment.

    Deletes the green environment and restores the original parameter
    group configuration.
    """
    from rds_migrate.migrate import rollback_deployment

    rds_client = get_rds_client(
        profile=ctx.obj["profile"],
        region=ctx.obj["region"],
    )
    result = rollback_deployment(
        rds_client=rds_client,
        deployment_id=deployment_id,
        delete_green=delete_green,
    )
    click.echo(result)


if __name__ == "__main__":
    cli()
