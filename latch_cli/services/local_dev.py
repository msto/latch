import asyncio
import json
import os
import re
import signal
import sys
import termios
import tty
from pathlib import Path
from typing import Callable, Optional, Union

import aioconsole
import asyncssh
import boto3
import websockets.client as websockets
import websockets.exceptions
from flytekit.core.context_manager import FlyteEntities
from flytekit.core.workflow import PythonFunctionWorkflow
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import NestedCompleter, PathCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from latch_cli.centromere.utils import import_flyte_objects
from latch_cli.config.latch import LatchConfig
from latch_cli.tinyrequests import post
from latch_cli.utils import (
    TemporarySSHCredentials,
    current_workspace,
    retrieve_or_login,
)

config = LatchConfig()
sdk_endpoints = config.sdk_endpoints

QUIT_COMMANDS = ["quit", "exit"]
EXEC_COMMANDS = ["shell", "run", "run-script"]
LIST_COMMANDS = ["ls", "list-tasks"]

COMMAND_EXPRESSION = re.compile("^(run\s.+|run-script\s.+|shell|list-tasks|ls.*)$")

COMMANDS = QUIT_COMMANDS + EXEC_COMMANDS + LIST_COMMANDS


def get_latest_image(pkg_root: Path) -> str:
    import_flyte_objects(paths=[pkg_root])

    wf_name = ""
    for entity in FlyteEntities.entities:
        if isinstance(entity, PythonFunctionWorkflow):
            wf_name = entity.name

    if wf_name == "":
        raise ValueError("Package root does not contain a workflow definition.")

    resp = post(
        sdk_endpoints["get-latest-version"],
        headers={"Authorization": f"Bearer {retrieve_or_login()}"},
        json={
            "wf_name": wf_name,
            "ws_account_id": current_workspace(),
        },
    )

    resp.raise_for_status()
    latest_version = resp.json()["version"]

    return f"{config.dkr_repo}/{current_workspace()}_{pkg_root.name}:{latest_version}"


async def copy_files(
    ssh_conn: asyncssh.SSHClientConnection,
    pkg_root: Path,
):
    # TODO(ayush) do something more sophisticated/only send over
    # diffs or smth to make this more efficient (rsync?)

    coroutines = []
    for path in [
        pkg_root.joinpath("wf"),
        pkg_root.joinpath("data"),
        pkg_root.joinpath("scripts"),
    ]:
        if path.exists():
            coroutines.append(
                asyncssh.scp(
                    path,
                    (ssh_conn, f"~/{pkg_root.name}"),
                    recurse=True,
                    block_size=16 * 1024,
                )
            )
        else:
            await aioconsole.aprint(f"Could not find {path} - skipping")

    if len(coroutines) > 0:
        await asyncio.gather(*coroutines)


async def get_messages(
    ws: websockets.WebSocketClientProtocol,
    show_output: bool,
):
    async for message in ws:
        msg = json.loads(message)
        if msg.get("Type") == "exit":
            return
        if show_output:
            await aioconsole.aprint(
                msg.get("Body"),
                end="",
                flush=True,
            )


async def send_message(
    ws: websockets.WebSocketClientProtocol,
    message: Union[str, bytes],
    typ: Optional[str] = None,
):
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if typ is None:
        typ = "body"
    body = {"Body": message, "Type": typ}
    await ws.send(json.dumps(body))
    # yield control back to the event loop,
    # see https://github.com/aaugustin/websockets/issues/867
    await asyncio.sleep(0)


async def send_resize_message(
    ws: websockets.WebSocketClientProtocol,
    term_width: int,
    term_height: int,
):
    await send_message(
        ws,
        json.dumps(
            {
                "Width": term_width,
                "Height": term_height,
            }
        ),
        typ="resize",
    )


async def run_local_dev_session(pkg_root: Path):
    scripts_dir = pkg_root.joinpath("scripts").resolve()

    def file_filter(filename: str) -> bool:
        file_path = Path(filename).resolve()
        return file_path == scripts_dir or scripts_dir in file_path.parents

    completer = NestedCompleter(
        {
            "run-script": PathCompleter(
                get_paths=lambda: [str(pkg_root)],
                file_filter=file_filter,
            ),
            "run": None,
            "ls": None,
            "list-tasks": None,
            "shell": None,
        },
        ignore_case=True,
    )
    session = PromptSession(
        ">>> ",
        completer=completer,
        complete_while_typing=True,
        auto_suggest=AutoSuggestFromHistory(),
        history=FileHistory(pkg_root.joinpath(".latch_history")),
    )

    key_path = pkg_root / ".ssh_key"

    with TemporarySSHCredentials(key_path) as ssh:
        headers = {"Authorization": f"Bearer {retrieve_or_login()}"}

        try:
            cent_resp = post(
                sdk_endpoints["provision-centromere"],
                headers=headers,
                json={"public_key": ssh.public_key},
            )
            cent_resp.raise_for_status()

            init_local_dev_resp = post(
                sdk_endpoints["local-development"],
                headers=headers,
                json={
                    "ws_account_id": current_workspace(),
                    "pkg_root": pkg_root.name,
                },
            )
            init_local_dev_resp.raise_for_status()
        except Exception as e:
            raise ValueError(f"Unable to provision a remote instance: {e}")

        if init_local_dev_resp.status_code == 403:
            raise ValueError("You are not authorized to use this feature.")

        try:
            cent_data = cent_resp.json()
            centromere_ip = cent_data["ip"]
            centromere_username = cent_data["username"]
        except KeyError as e:
            raise ValueError(f"Malformed response from provision endpoint: missing {e}")

        try:
            init_local_dev_data = init_local_dev_resp.json()
            docker_info = init_local_dev_data["docker"]
            access_key = docker_info["tmp_access_key"]
            secret_key = docker_info["tmp_secret_key"]
            session_token = docker_info["tmp_session_token"]
            port = init_local_dev_data["port"]
        except KeyError as e:
            raise ValueError(
                f"Malformed response from initialization endpoint: missing {e}"
            )

        # ecr setup
        ws_id = current_workspace()
        if int(ws_id) < 10:
            ws_id = f"x{ws_id}"

        try:
            ecr_client = boto3.session.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                region_name="us-west-2",
            ).client("ecr")

            ecr_auth = ecr_client.get_authorization_token()
            docker_access_token = ecr_auth["authorizationData"][0]["authorizationToken"]

        except Exception as err:
            raise ValueError(f"unable to retrieve an ecr login token") from err

        async with asyncssh.connect(
            centromere_ip, username=centromere_username
        ) as ssh_conn:
            await aioconsole.aprint("Copying your local changes... ", end="")
            await copy_files(ssh_conn, pkg_root)
            await aioconsole.aprint("Done.")

            async for ws in websockets.connect(
                f"ws://{centromere_ip}:{port}/ws",
                close_timeout=0,
                extra_headers=headers,
            ):
                try:
                    await aioconsole.aprint(
                        "Successfully connected to remote instance."
                    )
                    try:
                        while True:
                            with session.input.raw_mode():
                                cmd: str = await session.prompt_async()

                            if cmd == "":
                                continue

                            if cmd in QUIT_COMMANDS:
                                raise EOFError

                            if not COMMAND_EXPRESSION.match(cmd):
                                await aioconsole.aprint("Invalid command")
                                continue

                            if (
                                cmd.startswith("run")
                                or cmd == "list-tasks"
                                or cmd == "shell"
                            ):
                                await aioconsole.aprint("Syncing local changes... ")
                                await copy_files(ssh_conn, pkg_root)
                                await aioconsole.aprint("Finished syncing.")

                            await send_message(ws, cmd)

                            image_name_tagged = get_latest_image(pkg_root)
                            await send_message(ws, docker_access_token)
                            await send_message(ws, image_name_tagged)
                            await aioconsole.aprint(f"Pulling {image_name_tagged}... ")

                            await get_messages(ws, show_output=False)
                            await get_messages(ws, show_output=False)
                            await aioconsole.aprint("Image successfully pulled.\n")

                            await get_messages(ws, show_output=True)

                            if cmd == "shell":
                                with session.input.detach():
                                    with patch_stdout(raw=True):
                                        await shell_session(ws)
                                        await ws.drain()
                    except websockets.exceptions.ConnectionClosed:
                        continue
                except (KeyboardInterrupt, EOFError):
                    await aioconsole.aprint("Exiting local development session")
                    await ws.close()
                    return
                finally:
                    close_resp = post(
                        sdk_endpoints["close-local-development"],
                        headers={"Authorization": f"Bearer {retrieve_or_login()}"},
                    )
                    close_resp.raise_for_status()


async def shell_session(
    ws: websockets.WebSocketClientProtocol,
):
    old_settings_stdin = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin)

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(loop=loop)

    stdin2 = os.dup(sys.stdin.fileno())
    stdout2 = os.dup(sys.stdout.fileno())

    old_sigwinch_handler = signal.getsignal(signal.SIGWINCH)

    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(
            reader,
            loop=loop,
        ),
        os.fdopen(stdin2),
    )

    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        os.fdopen(stdout2),
    )
    writer = asyncio.streams.StreamWriter(
        writer_transport,
        writer_protocol,
        None,
        loop,
    )

    resize_event_queue = asyncio.Queue()

    def new_sigwinch_handler(signum, frame):
        if isinstance(old_sigwinch_handler, Callable):
            old_sigwinch_handler(signum, frame)

        term_width, term_height = os.get_terminal_size()
        resize_event_queue.put_nowait((term_width, term_height))

    signal.signal(signal.SIGWINCH, new_sigwinch_handler)

    async def resize_task():
        while True:
            (term_width, term_height) = await resize_event_queue.get()
            await send_resize_message(ws, term_width, term_height)

    async def input_task():
        while True:
            message = await reader.read(1)
            await send_message(ws, message)

    async def output_task():
        async for output in ws:
            obj: dict = json.loads(output)
            if obj.get("Type") == "exit":
                io_task.cancel()
                return

            message = obj.get("Body")
            if isinstance(message, str):
                message = message.encode("utf-8")
            writer.write(message)
            await writer.drain()
            await asyncio.sleep(0)

    try:
        io_task = asyncio.gather(input_task(), output_task(), resize_task())
        await io_task
    except asyncio.CancelledError:
        ...
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old_settings_stdin)
        signal.signal(signal.SIGWINCH, old_sigwinch_handler)


def local_development(pkg_root: Path):
    asyncio.run(run_local_dev_session(pkg_root))
