import asyncio
import json
import logging
import shlex
import sys

from ...api.requests import *
from ...config import AppConfig, AccessConfig
from ...util.rpc import AsyncRPCBridge
from ...util.dumbcode import to_json, from_json
from .command import Nonsense, CLICommand


class CommandWebsocket(CLICommand):
    """moggie websocket [<URL>]

    This establishes a websocket connection to a running moggie server,
    sending input received over STDIN and echoing back any messages from
    the server.

    URLs for connecting as different users/roles can be obtained using:

        moggie grant --output=urls

    If no URL is specified, the tool connects to the user's default local
    moggie (with unlimited access).

    ### Options

    %(OPTIONS)s

    """
    NAME = 'websocket'
    ROLES = AccessConfig.GRANT_ACCESS  # FIXME: Allow user to see own contexts?
    WEBSOCKET = False
    AUTO_START = False
    WEB_EXPOSE = False
    OPTIONS = [[
        ('--friendly',   [False], 'Enable the user friendly input mode'),
        ('--exit-after=', [None], 'X=maximum number of received messages')]]

    def __init__(self, *args, **kwargs):
        self.ws_url = None
        self.ws_tls = False
        self.ws_hostport = None
        self.ws_auth_token = None
        self.received = 0
        super().__init__(*args, **kwargs)

    def configure(self, args):
        args = self.strip_options(args)
        if len(args) > 1:
            raise Nonsense('Too many arguments')

        url = args[0] if (len(args) > 0) else None
        try:
            if url:
                proto, _, hostport, token = url.split('/', 3)
                if token[-1:] == '/':
                    token = token[:-1]
                if token[:1] == '@':
                    token = token[1:]
                if proto not in ('http:', 'https:'):
                    raise ValueError(proto)
                if not len(token) > 5:
                    raise ValueError(token)
                self.ws_tls = (proto == 'https:')
                self.ws_hostport = hostport
                self.ws_auth_token = token
                self.ws_url = 'ws%s://%s/ws' % (
                    's' if self.ws_tls else '', self.ws_hostport)
        except ValueError:
            import traceback
            traceback.print_exc()
            raise Nonsense('Invalid URL: %s' % url)

        return []

    def link_bridge(self, bridge):
        return self.handle_message

    def handle_message(self, bridge_name, message):
        if self.options['--friendly']:
            # Note: We don't use from_json() here, because we don't
            #       want to decode the binary data.
            print('<= ' + json.dumps(json.loads(message), indent=2))
        else:
            print('%s' % message)
        self.received += 1
        exit_after = self.options['--exit-after='][-1]
        if exit_after and self.received >= int(exit_after):
            sys.exit(1)

    async def read_json_loop(self, reader, bridge):
        pending = ''
        while True:
            data = await reader.read(1)
            if not data:
                break
            pending += str(data, 'utf-8')
            if '{' == pending[:1] and pending[-2:] == '}\n':
                try:
                    data = from_json(pending)
                    bridge.send(pending)
                except:
                    sys.stderr.write('Malformed input: %s\n'
                        % pending.replace('\n', ' '))
                    pending = ''

    async def read_friendly_loop(self, reader, bridge):
        pending = ''
        sys.stdout.write("""\
# Welcome to `moggie websockets` in friendly mode!
#
# Type your commands and they will be converted to JSON and sent. Examples:
#
#    count from:bre
#    search --limit=10 bjarni
#\n""")
        while True:
            data = await reader.read(1)
            if not data:
                break
            pending += str(data, 'utf-8')
            if pending.endswith('\n'):
                args = shlex.split(pending.strip())
                if args:
                    message = to_json({
                       'req_type': 'cli:%s' % args.pop(0),
                       'req_id': int(time.time()),
                       'args': args})
                    sys.stdout.write('=> %s\n' % message)
                    bridge.send(message)
                pending = ''

    async def run(self):
        ev_loop = asyncio.get_event_loop()

        if self.ws_url and self.ws_auth_token:
            bridge = AsyncRPCBridge(ev_loop, 'cli_websocket', None, self,
                ws_url=self.ws_url,
                auth_token=self.ws_auth_token)
            if self.options['--friendly']:
                print('##[ %s ]##\n#' % self.ws_url)
        else:
            app = self.connect()
            bridge = AsyncRPCBridge(ev_loop, 'cli_websocket', app, self)
            if self.options['--friendly']:
                print('##[ local moggie ]##\n#')

        async def connect_stdin_stdout(loop):
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            w_transport, w_protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, sys.stdout)
            writer = asyncio.StreamWriter(
                w_transport, w_protocol, reader, loop)
            return reader, writer

        reader, writer = await connect_stdin_stdout(ev_loop)
        try:
            if self.options['--friendly']:
                await self.read_friendly_loop(reader, bridge)
            else:
                await self.read_json_loop(reader, bridge)

        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            pass

