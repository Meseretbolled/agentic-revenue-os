import typer

app = typer.Typer(help="Agentic Revenue OS command line.")


@app.callback()
def root() -> None:
    """Agentic Revenue OS command line."""


if __name__ == "__main__":
    app()
