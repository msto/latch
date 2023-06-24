import re
import urllib.parse
from pathlib import Path
from urllib.parse import urljoin, urlparse

import click
from latch_sdk_config.user import user_config

from latch_cli.services.cp.exceptions import PathResolutionError
from latch_cli.services.cp.utils import get_auth_header

# todo(ayush): need a better way to check if "latch" has been appended to urllib
if "latch" not in urllib.parse.uses_netloc:
    urllib.parse.uses_netloc.append("latch")
    urllib.parse.uses_relative.append("latch")


latch_url_regex = re.compile(r"^(latch)?://")


def is_remote_path(path: str) -> bool:
    return latch_url_regex.match(path) is not None


def urljoins(*args: str, dir: bool = False) -> str:
    """Construct a URL by appending paths

    Paths are always joined, with extra `/`s added if missing. Does not allow
    overriding basenames as opposed to normal `urljoin`. Whether the final
    path ends in a `/` is still significant and will be preserved in the output

    >>> urljoin("latch:///directory/", "another_directory")
    latch:///directory/another_directory
    >>> # No slash means "another_directory" is treated as a filename
    >>> urljoin(urljoin("latch:///directory/", "another_directory"), "file")
    latch:///directory/file
    >>> # Unintentionally overrode the filename
    >>> urljoins("latch:///directory/", "another_directory", "file")
    latch:///directory/another_directory/file
    >>> # Joined paths as expected

    Args:
        args: Paths to join
        dir: If true, ensure the output ends with a `/`
    """

    res = args[0]
    for x in args[1:]:
        if res[-1] != "/":
            res = f"{res}/"
        res = urljoin(res, x)

    if dir and res[-1] != "/":
        res = f"{res}/"

    return res


scheme = re.compile(
    r"""
    ^(
        (latch://) |
        (file://) |
        (?P<implicit_url>://) |
        (?P<absolute_path>/) |
        (?P<relative_path>[^/])
    )
    """,
    re.VERBOSE,
)
domain = re.compile(
    r"""
    (?P<account_relative>
        ^$| # empty
        (?P<shared>^shared$)
    ) |
    (
        ^
        (
            (shared\.\d+\.account) |
            (\d+\.account) |
            ([^/]+\.mount) |
            (\d+\.node)
        )
        $
    )
    """,
    re.VERBOSE,
)


# scheme inference rules:
#   ://domain/a/b/c => latch://domain/a/b/c
#   /a/b/c => file:///a/b/c
#   a/b/c => file://${pwd}/a/b/c
def append_scheme(path: str) -> str:
    match = scheme.match(path)
    if match is None:
        raise PathResolutionError(f"{path} is not in a valid format")

    if match["implicit_url"] is not None:
        path = f"latch{path}"
    elif match["absolute_path"] is not None:
        path = f"file://{path}"
    elif match["relative_path"] is not None:
        path = f"file://{Path.cwd()}/{path}"

    return path


# domain inference rules:
#   latch:///a/b/c => latch://xxx.account/a/b/c
#   latch://shared/a/b/c => latch://shared.xxx.account/a/b/c
#   latch://any_other_domain/a/b/c => unchanged
def append_domain(path: str) -> str:
    workspace = user_config.workspace_id

    parsed = urlparse(path)
    dom = parsed.netloc

    if dom == "" and workspace != "":
        dom = f"{workspace}.account"

    match = domain.match(dom)
    if match is None:
        raise PathResolutionError(f"{dom} is not a valid path domain")

    if match["shared"] is not None and workspace != "":
        dom = f"shared.{workspace}.account"

    return parsed._replace(netloc=dom).geturl()


strip_domain_expr = re.compile(r"^(latch)?://[^/]*/")


def strip_domain(path: str) -> str:
    return strip_domain_expr.sub("", path, 1)


def is_account_relative(path: str) -> bool:
    parsed = urlparse(path)
    dom = parsed.netloc

    match = domain.match(dom)
    if match is None:
        raise PathResolutionError(f"{dom} is not a valid path domain")

    return match["account_relative"] is not None


def normalize_path(path: str) -> str:
    path = append_scheme(path)

    if path.startswith("file://"):
        return path

    return append_domain(path)


auth = re.compile(
    r"""
    ^(
        (?P<sdk>Latch-SDK-Token) |
        (?P<execution>Latch-Execution-Token)
    )\s.*$
""",
    re.VERBOSE,
)


def get_path_error(path: str, message: str, acc_id: str) -> PathResolutionError:
    with_scheme = append_scheme(path)
    normalized = normalize_path(path)

    account_relative = is_account_relative(with_scheme)

    auth_header = get_auth_header()
    match = auth.match(auth_header)
    if match is None:
        auth_type = auth_header
    elif match["sdk"] is not None:
        auth_type = "SDK Token"
    else:
        auth_type = "Execution Token"

    auth_str = (
        f"{click.style(f'Authorized using:', bold=True, reset=False)} {click.style(auth_type, bold=False, reset=False)}"
        + "\n"
    )

    ws_id = user_config.workspace_id
    ws_name = user_config.workspace_name

    resolve_str = (
        f"{click.style(f'Relative path resolved to:', bold=True, reset=False)} {click.style(normalized, bold=False, reset=False)}"
        + "\n"
    )
    ws_str = (
        f"{click.style(f'Using Workspace:', bold=True, reset=False)} {click.style(ws_id, bold=False, reset=False)}"
    )
    if ws_name is not None:
        ws_str = f"{ws_str} ({ws_name})"
    ws_str += "\n"

    return PathResolutionError(
        click.style(
            f"""
{click.style(f'{path}: ', bold=True, reset=False)}{click.style(message, bold=False, reset=False)}
{resolve_str if account_relative else ""}{ws_str if account_relative else ""}
{auth_str}
{click.style("Check that:", bold=True, reset=False)}
{click.style("1. The target object exists", bold=False, reset=False)}
{click.style(f"2. Account ", bold=False, reset=False)}{click.style(acc_id, bold=True, reset=False)}{click.style(" has permission to view the target object", bold=False, reset=False)}
{"3. The correct workspace is selected" if account_relative else ""}

For privacy reasons, non-viewable objects and non-existent objects are indistinguishable""",
            fg="red",
        )
    )


name = re.compile(r"^.*/(?P<name>[^/]+)/?$")


def get_name_from_path(path: str):
    match = name.match(path)
    if match is None:
        raise PathResolutionError(f"{path} is not a valid path")
    return match["name"]