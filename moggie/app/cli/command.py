import asyncio
import copy
import json
import logging
import time
import sys

from ...config import AccessConfig


class NotRunning(Exception):
    pass


class Nonsense(Exception):
    pass


class CLICommand:
    AUTO_START = False
    NAME = 'command'
    ROLES = AccessConfig.GRANT_ALL
    OPTIONS = {}
    CONNECT = True
    WEBSOCKET = True

    HTML_HEADER = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>%(title)s - moggie</title>
 <link rel=stylesheet href="/themed/css/webui.css">
 <link rel=stylesheet href="/themed/css/%(command)s.css">
 <script language=javascript>moggie_state = %(state)s;</script>
</head><body><div class="content">
"""
    HTML_FOOTER = """
</div><script language=javascript src='/static/js/webui.js'></script></body></html>"""

    @classmethod
    def Command(cls, wd, args):
        try:
            return cls(wd, args, access=True).sync_run()
        except BrokenPipeError:
            return False
        except Nonsense as e:
            sys.stderr.write('%s failed: %s\n' % (cls.NAME, e))
            return False
        except Exception as e:
            logging.exception('%s failed' % cls.NAME)
            sys.stderr.write('%s failed: %s\n' % (cls.NAME, e))
            return False

    @classmethod
    async def WebRunnable(cls, app, access, frame, conn, req_env, args):
        def reply(msg, eof=False):
            if msg or eof:
                if isinstance(msg, (bytes, bytearray)):
                    return conn.sync_reply(frame, msg, eof=eof)
                else:
                    return conn.sync_reply(frame, bytes(msg, 'utf-8'), eof=eof)
        try:
            cmd_obj = cls(app.profile_dir, args,
                access=access, appworker=app, connect=False, req_env=req_env)
            cmd_obj.write_reply = reply
            cmd_obj.write_error = reply
            return cmd_obj
        except PermissionError:
            raise
        except:
            logging.exception('Failed %s' % cls.NAME)
            reply('', eof=True)

    def __init__(self, wd, args,
            access=None, appworker=None, connect=True, req_env=None):
        from ...workers.app import AppWorker
        from ...util.rpc import AsyncRPCBridge

        self.options = copy.deepcopy(self.OPTIONS)
        self.connected = False
        self.messages = []
        self.workdir = wd
        self.context = None
        self.stdin = sys.stdin

        if access is not True and not access and self.ROLES:
            raise PermissionError('Access denied')
        self.access = access
        if self.ROLES and '--context=' not in self.options:
            self.context = self.get_context('default')

        def _writer(stuff):
            if isinstance(stuff, str):
                return sys.stdout.write(stuff)
            else:
                return sys.stdout.buffer.write(stuff)
        self.write_reply = _writer
        self.write_error = _writer

        self.mimetype = 'text/plain; charset=utf-8'
        self.webui_state = {'command': self.NAME}
        if req_env is not None:
            self.set_web_defaults(req_env)

        if connect and self.CONNECT:
            self.worker = AppWorker.FromArgs(wd, self.configure(args))
            if not self.worker.connect(autostart=self.AUTO_START, quick=True):
                raise NotRunning('Failed to launch or connect to app')
        else:
            self.configure(args)
            self.worker = appworker

        self.ev_loop = asyncio.get_event_loop()
        if connect and self.WEBSOCKET:
            self.app = AsyncRPCBridge(self.ev_loop, 'cli', self.worker, self)
            self.ev_loop.run_until_complete(self._await_connection())

    def set_web_defaults(self, req_env):
        self.stdin = []
        if '--format=' in self.options:
            ua = (req_env.http_headers.get('User-Agent')
                or req_env.http_headers.get('user-agent')
                or '')
            at = req_env.http_headers.get('Accept') or ''

            if at.startswith('text/plain'):
                self.options['--format='][:1] = ['text']
            elif at.startswith('text/html'):
                self.options['--format='][:1] = ['html']
            elif ('json' in at) or ('Mozilla' not in ua):
                self.options['--format='][:1] = ['json']

    def print_sexp(self, data, nl='\n'):
        def _sexp(exp):
            out = ''
            if isinstance(exp, (list, tuple)):
                out += '(' + ' '.join(_sexp(x) for x in exp) + ')'
            elif isinstance(exp, dict):
                out += ('(' + ' '.join(
                        ':%s %s' % (k, _sexp(v)) for k, v in exp.items())
                    + ')')
            elif isinstance(exp, str):
                out += '"%s"' % repr(exp)[1:-1].replace('"', '\"')
            elif exp in (False, None):
                out += 'nil'
            elif exp is True:
                out += 't'
            else:
                out += '%s' % exp
            return out
        self.write_reply(_sexp(data) + nl)

    def print_html_start(self, html='', title=None, state=None):
        self.write_reply((self.HTML_HEADER % {
            'title': title or self.NAME,
            'command': self.NAME,
            'state': json.dumps(state or self.webui_state)}) + html)

    def print_html_end(self, html=''):
        self.print(html + self.HTML_FOOTER)

    def print_html_tr(self, row, columns=None):
        self.write_reply(self.format_html_tr(row, columns))

    def format_html_tr(self, row, columns=None):
        def _esc(data):
            if isinstance(data, list):
                return ' '.join(_esc(d) for d in data if d)
            elif isinstance(data, int):
                return '%d' % data
            elif isinstance(data, float):
                return '%.3f' % data
            elif isinstance(data, str):
                return (data
                    .replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
            else:
                return ''

        def _link(k, text):
            url = row.get('_url_' + k)
            if url:
                return '<a href="%s">%s</a>' % (url, text)
            else:
                return text

        columns = columns or row.keys()
        return ('<tr>%s</tr>' % ''.join(
            '<td class=%s>%s</td>' % (k, _link(k, _esc(row[k])))
            for k in columns if k in row))

    def print_json(self, data, nl='\n'):
        self.write_reply(json.dumps(data) + nl)

    def print(self, *args, nl='\n'):
        self.write_reply(' '.join(args) + nl)

    def error(self, *args, nl='\n'):
        self.write_error(' '.join(args) + nl)

    async def _await_connection(self):
        sleeptime, deadline = 0, (time.time() + 10)
        while time.time() < deadline:
            sleeptime = min(sleeptime + 0.01, 0.1)
            await asyncio.sleep(sleeptime)
            if self.connected:
                break

    def link_bridge(self, bridge):
        def _receive_message(bridge_name, raw_message):
            message = json.loads(raw_message)
            if message.get('connected'):
                self.connected = True
            else:
                self.handle_message(message)
        return _receive_message

    def handle_message(self, message):
        self.messages.append(message)

    def get_context(self, ctx=None):
        from ...config import AppConfig
        cfg = AppConfig(self.workdir)

        all_contexts = cfg.contexts
        ctx = ctx or (self.options.get('--context=') or ['default'])[-1]
        if ctx == 'default':
            if self.access is True or not self.access:
                ctx = cfg.get(
                    AppConfig.GENERAL, 'default_cli_context', fallback='Context 0')
            else:
                ctx = self.access.get_default_context()
        else:
            if ctx not in all_contexts:
                for ctx_key, ctx_info in all_contexts.items():
                    if ctx == ctx_info.name:
                        ctx = ctx_key
                        break
        if ctx not in all_contexts:
            raise Nonsense('Invalid context: %s' % ctx)

        if self.ROLES and self.access is not True:
            if not self.access.grants(ctx, self.ROLES):
                logging.error('Access denied, need %s on %s' % (self.ROLES, ctx))
                raise PermissionError('Access denied')

        return ctx

    def metadata_worker(self):
        from ...workers.metadata import MetadataWorker
        return MetadataWorker.Connect(self.worker.worker_dir)

    def search_worker(self):
        from ...workers.search import SearchWorker
        return SearchWorker.Connect(self.worker.worker_dir)

    def strip_options(self, args):
        # This should be compatible-ish with how notmuch does things.
        leftovers = []
        def _setopt(name, val):
            if name not in self.options:
                self.options[name] = []
            self.options[name].append(val)
        while args:
            arg = args.pop(0)
            if arg == '--':
                leftovers.extend(args)
                break
            elif arg+'=' in self.OPTIONS:
                _setopt(arg+'=', args.pop(0))
            elif arg in self.OPTIONS:
                _setopt(arg, True)
            elif arg[:2] == '--':
                if '=' in arg:
                    arg, opt = arg.split('=', 1)
                else:
                    try:
                        arg, opt = arg.split(':', 1)
                    except ValueError:
                        pass
                if arg+'=' not in self.OPTIONS:
                    raise Nonsense('Unrecognized argument: %s' % arg)
                _setopt(arg+'=', opt)
            else:
                leftovers.append(arg)
        if '--context=' in self.options:
            self.context = self.get_context()
        return leftovers

    def configure(self, args):
        return args

    async def await_messages(self, *prototypes, timeout=10):
        sleeptime, deadline = 0, (time.time() + timeout)
        while time.time() < deadline:
            if not self.messages:
                sleeptime = min(sleeptime + 0.01, 0.1)
                await asyncio.sleep(sleeptime)
            while self.messages:
                msg = self.messages.pop(0)
                if msg.get('prototype') in prototypes:
                    return msg
        return {}

    async def run(self):
        raise Nonsense('Unimplemented')

    async def web_run(self):
        try:
            return await self.run()
        except PermissionError:
            logging.info('Access denied in %s' % self.NAME)
        except:
            logging.exception('Failed to run %s' % self.NAME)
        finally:
            try:
                self.write_reply('', eof=True)
            except:
                pass

    def sync_run(self):
        task = asyncio.ensure_future(self.run())
        while not task.done():
            try:
                self.ev_loop.run_until_complete(task)
            except KeyboardInterrupt:
                if task:
                    task.cancel()
