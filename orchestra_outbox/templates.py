"""Templates: a kind's content, and a kind's payload, rendered from a draft's fields.

Placeholders are ``$name`` or ``${name}`` (Python's ``string.Template``: no expressions, no
attribute access, ``$$`` for a dollar). ``$content`` is the drafter's content (or, in a payload, the
content as finally approved, perhaps edited by the owner). A missing field is the drafter's
mistake and refuses the draft, naming the field.

In a payload (``jira.create``'s issue fields), every string is rendered; a string that is exactly
one placeholder (``"$sprint"``) takes the field's value as it is, so a number stays a number."""

from __future__ import annotations

import re
from string import Template

from .kinds import DraftError

MAX_FIELDS = 50
MAX_FIELD = 20_000
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WHOLE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


def fields(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_FIELDS:
        raise DraftError(f"fields must be an object of at most {MAX_FIELDS} entries")
    for k, v in value.items():
        if not _KEY.match(k) or k == "content":
            raise DraftError(f"field {k!r}: a name is letters, digits and _, and not 'content'")
        if isinstance(v, bool) or not isinstance(v, str | int | float) or (isinstance(v, str) and len(v) > MAX_FIELD):
            raise DraftError(f"field {k!r} must be a string (at most {MAX_FIELD} characters) or a number")
    return dict(value)


def placeholders(template: str) -> set[str]:
    """The names a template uses (for ``check``); raises ValueError for a malformed one."""
    t = Template(template)
    if not t.is_valid():
        raise ValueError("a malformed placeholder: use $name or ${name}, and $$ for a dollar")
    return set(t.get_identifiers())


def render(template: str, values: dict) -> str:
    try:
        return Template(template).substitute({k: str(v) for k, v in values.items()})
    except KeyError as e:
        raise DraftError(f"the kind's template needs the field {e.args[0]!r}") from None
    except ValueError:
        raise DraftError("the kind's template has a malformed placeholder") from None


def render_payload(payload: object, values: dict) -> object:
    if isinstance(payload, dict):
        return {k: render_payload(v, values) for k, v in payload.items()}
    if isinstance(payload, list):
        return [render_payload(v, values) for v in payload]
    if isinstance(payload, str):
        if m := _WHOLE.match(payload):
            name = m.group(1) or m.group(2)
            if name not in values:
                raise DraftError(f"the kind's payload needs the field {name!r}")
            return values[name]
        return render(payload, values)
    return payload
