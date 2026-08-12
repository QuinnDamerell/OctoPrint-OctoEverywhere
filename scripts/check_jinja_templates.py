"""Parse every Jinja2 template in the repository to catch syntax errors."""

from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRECTORIES = {".git", "build", "dist", "node_modules", "py2venv", "py3venv"}


def main() -> int:
    templates = sorted(
        path
        for path in REPOSITORY_ROOT.rglob("*.jinja2")
        if EXCLUDED_DIRECTORIES.isdisjoint(path.relative_to(REPOSITORY_ROOT).parts)
    )
    if not templates:
        print("No Jinja2 templates found.")
        return 1

    environment = Environment(autoescape=True)
    errors = 0
    for template in templates:
        relative_path = template.relative_to(REPOSITORY_ROOT)
        try:
            environment.parse(template.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TemplateSyntaxError) as error:
            line_number = getattr(error, "lineno", 1)
            print(f"{relative_path}:{line_number}: {error}")
            errors += 1

    if errors:
        print(f"Jinja2 syntax check failed with {errors} error(s).")
        return 1

    print(f"Parsed {len(templates)} Jinja2 template(s) successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
