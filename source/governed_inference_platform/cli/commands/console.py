# ABOUTME: `gip console` command — launches the localhost visual deployment console
# ABOUTME: Thin wrapper around governed_inference_platform.console.server (stdlib HTTP)

"""Console command - local visual deployment console (wizard UI over init + deploy)."""

import webbrowser

from cleo.commands.command import Command
from cleo.helpers import option
from rich.console import Console


class ConsoleCommand(Command):
    name = "console"
    description = (
        "Launch the local visual deployment console (browser wizard over 'gip init --from-file' + 'gip deploy')"
    )

    options = [
        option("port", None, description="Port to serve the console on (127.0.0.1 only)", flag=False, default="8321"),
        option("no-browser", None, description="Do not auto-open the browser", flag=True),
    ]

    def handle(self) -> int:
        """Execute the console command."""
        console = Console()

        try:
            port = int(self.option("port"))
        except (TypeError, ValueError):
            console.print(f"[red]Invalid port: {self.option('port')}[/red]")
            return 1
        if not (0 <= port <= 65535):
            console.print(f"[red]Port must be between 0 and 65535 (got {port}).[/red]")
            return 1

        from governed_inference_platform.console.server import create_console_server

        try:
            server = create_console_server(port=port)
        except OSError as e:
            console.print(f"[red]Could not bind 127.0.0.1:{port}: {e}[/red]")
            console.print("[dim]Try another port: gip console --port 8322[/dim]")
            return 1

        console.print("\n[bold cyan]Governed Inference Platform — Deployment Console[/bold cyan]")
        console.print(f"  Serving on: [bold]{server.url}[/bold] (localhost only)")
        console.print(f"  Session token: [dim]{server.token}[/dim]")
        console.print("  [dim]The token is embedded in the page; API calls without it are rejected.[/dim]")
        console.print("  Press [bold]Ctrl+C[/bold] to stop.\n")

        if not self.option("no-browser"):
            try:
                webbrowser.open_new_tab(server.url)
            except Exception:
                console.print("[yellow]Could not open a browser automatically — open the URL above manually.[/yellow]")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[yellow]Console stopped.[/yellow]")
        finally:
            server.server_close()
        return 0
