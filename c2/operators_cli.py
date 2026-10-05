"""Operator management CLI.

    flask --app app operators                 list
    flask --app app operators add NAME        create, prints the token once
    flask --app app operators passwd NAME     set a new password
    flask --app app operators token NAME      rotate the token
    flask --app app operators revoke NAME     drop the token
    flask --app app operators disable NAME    keep the row, block access
    flask --app app operators enable NAME     undo that
    flask --app app operators rm NAME         delete the row

Passwords come from ``--password`` or the ``C2_NEW_PASSWORD`` environment
variable, never from a command-line default, so they do not land in shell
history.
"""

from __future__ import annotations

import os

import click

from c2 import operators as ops
from c2.timeutil import utcnow
from database import db
from models import Operator


def _password(prompt: str) -> str:
    supplied = os.environ.get("C2_NEW_PASSWORD")
    if supplied:
        return supplied
    return click.prompt(prompt, hide_input=True, confirmation_prompt=True)


@click.group("operators")
def cli():
    """Manage panel operators."""


@cli.command("list")
def list_operators():
    rows = Operator.query.order_by(Operator.name.asc()).all()
    if not rows:
        click.echo("no operators yet")
        return
    click.echo(f"{'name':<20} {'role':<8} {'token':<10} {'disabled':<9} last login")
    for r in rows:
        click.echo(f"{r.name:<20} {r.role:<8} "
                   f"{(r.token_fingerprint() or '-'):<10} "
                   f"{str(bool(r.disabled)):<9} "
                   f"{r.last_login.isoformat() if r.last_login else '-'}")


@cli.command("add")
@click.argument("name")
@click.option("--role", default="admin",
              type=click.Choice(["admin", "viewer"]), show_default=True)
def add(name, role):
    """Create an operator. The token is shown once."""
    try:
        row, token = ops.create_operator(name, _password(f"password for {name}"), role)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"created {row.name} ({row.role})")
    click.echo(f"token: {token}")
    click.echo("this is the only time it is shown — store it now")


@cli.command("passwd")
@click.argument("name")
def passwd(name):
    """Replace an operator's password."""
    try:
        ops.set_password(name, _password(f"new password for {name}"))
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"password updated for {name}")


@cli.command("token")
@click.argument("name")
def token(name):
    """Rotate an operator's API token."""
    try:
        row, new = ops.issue_token(name)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"new token for {row.name}: {new}")
    click.echo("the previous token stopped working immediately")


@cli.command("revoke")
@click.argument("name")
def revoke(name):
    """Drop an operator's API token, leaving password login intact."""
    try:
        ops.revoke_token(name)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"token revoked for {name}")


@cli.command("disable")
@click.argument("name")
def disable(name):
    """Block all access for an operator without losing their audit history."""
    try:
        ops.disable(name, True)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{name} disabled")


@cli.command("enable")
@click.argument("name")
def enable(name):
    try:
        ops.disable(name, False)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{name} enabled")


@cli.command("rm")
@click.argument("name")
@click.option("--yes", is_flag=True, help="skip the confirmation")
def remove(name, yes):
    """Delete an operator row. Their audit entries stay."""
    try:
        row = ops.require(name)
    except ops.AuthError as exc:
        raise click.ClickException(str(exc))
    if not yes:
        click.confirm(f"delete operator {row.name}?", abort=True)
    ops.delete(name)
    click.echo(f"removed {name}; audit history retained")


def register(app):
    app.cli.add_command(cli)
