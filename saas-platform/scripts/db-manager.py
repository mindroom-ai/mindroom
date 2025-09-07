#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich", "httpx", "python-dotenv"]
# ///

"""Unified Database Management Tool."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

# Load environment variables
load_dotenv()

# Initialize Rich console and Typer app
console = Console()
app = typer.Typer(help="Database management tool for MindRoom SaaS platform")

# Environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

# Check required environment variables
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    console.print("[red]❌ Missing required environment variables:[/red]")
    console.print("   SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    sys.exit(1)

# Expected tables
EXPECTED_TABLES = [
    "accounts",
    "subscriptions",
    "instances",
    "usage_metrics",
    "webhook_events",
    "audit_logs",
]

# Table order for reset (respects foreign key constraints)
RESET_TABLE_ORDER = [
    "audit_logs",
    "webhook_events",
    "usage_metrics",
    "instances",
    "subscriptions",
    "accounts",
]


def get_migrations_dir() -> Path:
    """Get the migrations directory path."""
    return Path(__file__).parent.parent / "supabase" / "migrations"


def check_tables() -> list[str]:
    """Check which tables exist in the database."""
    try:
        response = httpx.get(
            f"{SUPABASE_URL}/rest/v1/",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
        )
        data = response.json()
        return list(data.get("definitions", {}).keys())
    except Exception:
        return []


def check_rls() -> bool:
    """Check if Row Level Security is enabled."""
    try:
        # Try to access with anon key
        response = httpx.get(
            f"{SUPABASE_URL}/rest/v1/accounts?limit=1",
            headers={"apikey": SUPABASE_ANON_KEY},
        )
        data = response.json()
        # If we get an error or message, RLS is likely enabled
        return not isinstance(data, list) or "message" in data
    except Exception:
        return True  # Error likely means RLS is blocking


def get_migrations() -> list[dict]:
    """Get list of migration files."""
    migrations_dir = get_migrations_dir()
    if not migrations_dir.exists():
        return []

    sql_files = sorted(migrations_dir.glob("*.sql"))
    migrations = []

    for file in sql_files:
        content = file.read_text()
        # Check if this migration is applied (simplified check)
        applied = file.name == "001_schema.sql" and len(check_tables()) > 0

        migrations.append(
            {
                "file": file.name,
                "path": f"supabase/migrations/{file.name}",
                "content": content[:500] + ("..." if len(content) > 500 else ""),
                "full_content": content,
                "applied": applied,
            },
        )

    return migrations


@app.command()
def status() -> None:
    """Check database status and migrations."""
    console.print(Panel.fit("[cyan]📊 Database Status[/cyan]", border_style="cyan"))

    # Check tables
    existing_tables = check_tables()
    table = Table(title="Tables", show_header=False)
    table.add_column("Status", style="dim", width=3)
    table.add_column("Table")

    for table_name in EXPECTED_TABLES:
        if table_name in existing_tables:
            table.add_row("✅", f"[green]{table_name}[/green]")
        else:
            table.add_row("❌", f"[red]{table_name} (missing)[/red]")

    console.print(table)

    # Check RLS
    console.print("\n[bold]Security:[/bold]")
    if check_rls():
        console.print("  ✅ Row Level Security enabled")
    else:
        console.print("  [yellow]⚠️  Row Level Security NOT enabled[/yellow]")
        console.print("     Run: [cyan]./db.sh apply[/cyan]")

    # Show migrations
    console.print("\n[bold]Migrations:[/bold]")
    migrations = get_migrations()
    for m in migrations:
        status_icon = "✅" if m["applied"] else "⏳"
        status_color = "green" if m["applied"] else "yellow"
        console.print(f"  {status_icon} [{status_color}]{m['file']}[/{status_color}]")


@app.command()
def apply() -> None:
    """Show SQL to apply migrations."""
    console.print(Panel.fit("[cyan]📋 Database Migrations[/cyan]", border_style="cyan"))

    migrations = get_migrations()
    unapplied = [m for m in migrations if not m["applied"]]

    if not unapplied:
        console.print("[green]✅ All migrations are applied![/green]")
        return

    console.print("To apply migrations, run these in Supabase Dashboard SQL Editor:\n")

    for migration in unapplied:
        console.print(f"[yellow]-- File: {migration['path']}[/yellow]")
        console.print("─" * 60)

        # Use Syntax for SQL highlighting
        syntax = Syntax(migration["full_content"], "sql", theme="monokai", line_numbers=True)
        console.print(syntax)
        console.print("─" * 60)
        console.print()

    console.print("[cyan]After applying, run: ./db.sh status[/cyan]")


@app.command()
def reset(force: bool = typer.Option(False, "--force", help="Confirm database reset")) -> None:
    """Reset database (delete all data)."""
    if not force:
        console.print("[yellow]⚠️  WARNING: This will DELETE ALL DATA![/yellow]")
        console.print("Run with --force to confirm: [cyan]./db.sh reset --force[/cyan]")
        raise typer.Exit(1)

    console.print("[red]🗑️  Resetting database...[/red]\n")

    # Create Supabase client headers
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for table_name in RESET_TABLE_ORDER:
        try:
            # Delete all records from table
            response = httpx.delete(
                f"{SUPABASE_URL}/rest/v1/{table_name}",
                headers=headers,
                params={"id": "neq.00000000-0000-0000-0000-000000000000"},
            )

            if response.status_code in [200, 204]:
                console.print(f"  ✅ Cleared {table_name}")
            elif response.status_code == 404:
                console.print(f"  [yellow]⚠️[/yellow]  {table_name}: Table not found")
            else:
                console.print(f"  [yellow]⚠️[/yellow]  {table_name}: {response.text}")
        except Exception as e:
            console.print(f"  [red]❌[/red] {table_name}: {e!s}")

    console.print("\n[green]✅ Database reset complete[/green]")
    console.print("Run [cyan]./db.sh apply[/cyan] to reapply schema")


@app.command()
def stripe() -> None:
    """Setup Stripe products and pricing."""
    console.print("[cyan]💳 Setting up Stripe products...[/cyan]\n")

    setup_script = Path(__file__).parent / "db" / "setup-stripe-products.js"
    if not setup_script.exists():
        console.print("[red]❌ Stripe setup script not found[/red]")
        raise typer.Exit(1)

    try:
        result = subprocess.run(
            ["node", str(setup_script)],
            check=False,
            capture_output=False,
            text=True,
        )
        if result.returncode == 0:
            console.print("\n[green]✅ Stripe products created[/green]")
        else:
            console.print("[red]❌ Stripe setup failed[/red]")
            raise typer.Exit(1)  # noqa: TRY301
    except Exception as e:
        console.print(f"[red]❌ Error running Stripe setup: {e}[/red]")
        raise typer.Exit(1)  # noqa: B904


@app.command()
def admin() -> None:
    """Create admin dashboard user."""
    console.print("[cyan]👤 Creating admin user...[/cyan]\n")

    admin_script = Path(__file__).parent / "db" / "create-admin-user.js"
    if not admin_script.exists():
        console.print("[red]❌ Admin setup script not found[/red]")
        raise typer.Exit(1)

    try:
        result = subprocess.run(
            ["node", str(admin_script)],
            check=False,
            capture_output=False,
            text=True,
        )
        if result.returncode == 0:
            console.print("\n[green]✅ Admin user created[/green]")
        else:
            console.print("[red]❌ Admin setup failed[/red]")
            raise typer.Exit(1)  # noqa: TRY301
    except Exception as e:
        console.print(f"[red]❌ Error creating admin user: {e}[/red]")
        raise typer.Exit(1)  # noqa: B904


if __name__ == "__main__":
    app()
