import re
import sys
import urwid

from ...email.metadata import Metadata
from ...email.addresses import AddressInfo
from ...jmap.requests import RequestEmail

from .widgets import *


class EmailDisplay(urwid.ListBox):
    COLUMN_NEEDS = 60
    COLUMN_WANTS = 70
    COLUMN_FIT = 'weight'
    COLUMN_STYLE = 'content'

    def __init__(self, tui_frame, metadata, parsed=None):
        self.tui_frame = tui_frame
        self.metadata = Metadata(*metadata)
        self.parsed = self.metadata.parsed()
        self.email = parsed
        self.uuid = self.metadata.uuid_asc
        self.crumb = self.parsed.get('subject', 'FIXME')

        self.rendered_width = self.COLUMN_NEEDS
        self.email_body = urwid.Text('(loading...)')
        self.widgets = urwid.SimpleListWalker(
            list(self.headers()) + [self.email_body])

        self.search_obj = RequestEmail(self.metadata, text=True)
        self.tui_frame.app_bridge.send_json(self.search_obj)

        urwid.ListBox.__init__(self, self.widgets)

    def headers(self):
        for field in ('Date:', 'To:', 'Cc:', 'From:', 'Reply-To:', 'Subject:'):
            fkey = field[:-1].lower()
            if fkey not in self.parsed:
                continue

            value = self.parsed[fkey]
            if not isinstance(value, list):
                value = [value]

            for val in value:
                if isinstance(val, AddressInfo):
                    if val.fn:
                        val = '%s <%s>' % (val.fn, val.address)
                    else:
                        val = '<%s>' % val.address
                else:
                    val = str(val).strip()
                if not val:
                    continue
                yield urwid.Columns([
                    ('fixed',  8, urwid.Text(('email_key_'+fkey, field), align='right')),
                    ('weight', 4, urwid.Text(('email_val_'+fkey, val)))],
                    dividechars=1)
                field = ''
        yield(urwid.Divider())

    def cleanup(self):
        del self.tui_frame
        del self.email

    def render(self, size, focus=False):
        self.rendered_width = size[0]
        return super().render(size, focus=focus)

    def incoming_message(self, message):
        from moggie.security.html import html_to_markdown

        def _to_md(txt):
            return html_to_markdown(txt,
                no_images=True,
                wrap=min(self.COLUMN_WANTS, self.rendered_width-1))

        if (message.get('prototype') != self.search_obj['prototype'] or
                message.get('req_id') != self.search_obj['req_id']):
            return
        self.email = message['email']

        email_txts = {'text/plain': '', 'text/html': ''}
        for ctype, fmt in (
                ('text/plain', lambda t: t),
                ('text/html',  _to_md)):
            for part in self.email['_PARTS']:
                if part['content-type'][0] == ctype:
                    email_txts[ctype] += fmt(part.get('_TEXT', ''))

        # This is a heuristic to avoid the case where silly people
        # send a plain-text part that says "there is no text part".
        len_html = len(email_txts['text/html'])
        len_text = len(email_txts['text/plain'])
        if len_html > 60:
            email_text = email_txts['text/html']
        else:
            email_text = email_txts['text/plain']

        email_text = re.sub(
            r'\n\s*\n', '\n\n', email_text.replace('\r', ''), flags=re.DOTALL)

        self.email_body = urwid.Text(email_text.strip())
        self.widgets[-1] = self.email_body
