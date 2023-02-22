# Relatively low level commands for generating or parsing/displaying e-mail.
#
# FIXMEs:
#   - message templates
#   - accept a JSON data structure instead of arguments?
#      - Can this be done in a generic way in the CLICommand class
#   - accept a text repro too? Hmm.
#   - accept base64 encoded data as attachment args - needed for web API
#   - fancier date parsing
#   - do we want to generate strong passwords for users when ZIP encrypting?
#   - add some styling to outgoing HTML e-mails?
#   - search for attachments? define a URL-to-an-attachment?
#   - get send-via settings from config/context/...
#
#   - implement PGP/MIME encryption, autocrypt
#   - implement PGP/MIME signatures
#   - implement DKIM signatures
#
import base64
import io
import os
import sys
import time

from .command import Nonsense, CLICommand, AccessConfig
from ...email.addresses import AddressInfo
from ...email.parsemime import MessagePart
from ...jmap.requests import RequestSearch, RequestEmail
from ...security.html import HTMLCleaner
from ...security.css import CSSCleaner


def _html_quote(t):
    return (t
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;'))


def _make_message_id(random_data=None):
    from binascii import hexlify
    return '<%s@mailpile>' % (
        str(hexlify(random_data or os.urandom(16)), 'utf-8'))


class CommandEmail(CLICommand):
    """moggie email [<options> ...]

    This command will generate (and optionally send) an e-mail, in
    accordance with the command-line options, Moggie settings, and relevant
    standards. It can be used as a stand-alone tool, but some features
    depend on having a live moggie backend.

    ## General options

    These options control the high-level behavior of this command; where
    it loads default settings from, what it does with the message once
    it has been generated, and how the output is formatted:

    %(moggie)s

    Note that by default, the e-mail is generated but not sent. If you
    want the e-mail to be sent you must specify `--send-at=` and/or
    `--send-via=`. Delayed sending requires the moggie backend be running
    at the requested time.

    The `--send-via=` command takes either server address, formatted as
    a URI (e.g. `smtp://user:pass@hostname:port/`), or the path to a
    local command to pipe the output too. Recognized protocols are `smtp`,
    `smtps` (SMTP over TLS) and `smtptls` (SMTP, requiring STARTTLS). The
    command specification can include Python `%%(...)s` formatting, for
    variables `from`, `to` (a single-argument comma-separated list) or
    `to_list` (one argument per recipient).

    ### Examples:

        # Generate an e-mail and dump to stdout, encapsulated in JSON:
        moggie email --format=json --subject="hello" --to=...

        # Use an alternate context for loading from, signature etc.
        moggie email --context='Work' --subject='meeting times' ... 

        # Send a message via SMTP to root@localhost
        moggie email [...] \\
            --send-via=smtp://localhost:25 \\
            --send-to=root@localhost 

        # Send a message using /usr/bin/sendmail
        moggie email [...] \\
            --send-via='/usr/bin/sendmail -f%%(from)s -- %%(to_list)s'


    ## Message headers

    Message headers can be specified on the command-line, using these
    options:

    %(headers)s

    If omitted, defaults are either loaded from the moggie configuration
    (for the active Context), derived from the headers of messages being
    forwarded or replied to or a best-effort combination of the two.

    ### Examples:

        # Set a subject and single recipient
        moggie email --subject='Hello friend!' --to=pal@exmaple.org ...

        # Set a custom header
        moggie email --header='X-Testing:Hello world' ...


    ## Message contents

    Moggie constructs e-mails with three main parts: a text part, an
    HTML part, and one or more attachments:

    %(content)s

    The most basic interface is to specify exactly the contents of
    each section using `--text=`, `--html=` and one or more `--attach=`
    options.

    Higher level usage is to use `--message=` and `--signature=` options to
    specify content, and let moggie take care of generating plain-text and
    HTML parts. This can be combined with replies, forwards or templates
    (see below) to generate quite complex messages according to common
    e-mail customs and best practices.

    ## Forwarding and replying

    The tool can automatically quote or forward other e-mails, reading
    them from local files (or stdin), or loading from the moggie search
    index and mail store. Options:

    %(searches)s

    The `--reply=` option is part of the high-level content generation;
    depending on the `--quoting=` option some or all of the text/HTML
    content of the replied-to messages will be included as quotes in the
    output message.

    When forwarding, the `inline` style (the `--forwarding=` option)
    is the default, where message text will be quoted in the message body,
    a default subject set and attachments will be re-attached to the new
    email. Specifying `--forwarding=attachment` will instead attach the
    the original mail unmodified as a `.eml` file with the `message/rfc822`
    MIME-type. Using `--forwarding=bounce` will output the original
    forwarded message entirely unmodified, aside from adding headers
    indicating it has been resent. Note that bounce forwarding (resending)
    is incompatible with most other `moggie email` features.

    **NOTE:** Be careful when using search terms with `--reply=` and
    `--forward=`, since searches matching multiple e-mails can result
    in very large output with unexpected contents. Sticking with
    `id:...` and/or tag-based searches is probably wise. If you are
    deliberately forwarding multiple messages, it may be a good idea to
    send them as a .ZIP archive (see encryption options below).

    ## Encryption, signatures, archives

    Moggie supports two forms of encryption, AES-encrypted ZIP archives
    and OpenPGP (PGP/MIME). Moggie also supports two types of digital
    signatures, DKIM and OpenPGP (PGP/MIME).

    %(encryption)s

    Note that default signing and encrypting preferences may be
    configured by the active moggie context.

    ZIP encryption is useful for sending confidential messages to people
    who do not have OpenPGP keys. All mainstream operating systems either
    include native support for AES-encrypted ZIP files, or have widely
    available free tools which do so. However, users should be made aware
    that encrypted ZIP files leak a significant amount of metadata about
    their contents, and communicating the password will have to be done
    using a side-channel (e.g. a secure message or phone call). These
    archives are only as secure as the passwords and the channels used to
    transmit them.

    ### Examples:

        # Encrypt to a couple of OpenPGP keys; note it is the caller's
        # job to ensure the e-mail recipients match. The keys must be
        # on the caller's GnuPG keychain.
        moggie email [...] \\
            --encrypt=all \\
            --encrypt-to=PGP:61A015763D28D410A87B197328191D9B3B4199B4 \\
            --encrypt-to=PGP:CB484157EC53EEE53C1369C3C5728DA522425313

        # Sign using both OpenPGP and DKIM identities
        moggie email [...] \\
            --sign-as=PGP:61A015763D28D410A87B197328191D9B3B4199B4 \\
            --sign-as=DKIM:/path/to/secret-key

        # Put the attachments in an encrypted ZIP file
        moggie email [...] \\
            --encrypt=attachments \\
            --zip-password="super-strong-secret-diceware-passphrase"

    As a convenience, a ZIP password of 'NONE' will generate an
    unencrypted ZIP archive, in case you just want moggie to generate
    a ZIP file for you:

        moggie email [...] \\
            --encrypt=attachments --zip-password=NONE

    ## Known bugs and limitations

    A bunch of the stuff above isn't yet implemented. It's a lot!
    This is a work in progress.

    """
    _NOTES = """

     - Oops, what we actually do is generate the message itself.
     - We want the message template for notmuch compat
     - Being able to generate full messages is more useful though
     - For proper email clients a JSON (or sexp) representation is
       desirable, but we need to be able to receive it back and work
       with that instead of command line args.
     - Do we care to support primitive composition? It's mutt/unix-like
       but gets quite faffy.

    TODO:
     - Think about output formats
     - Accept our output as input?
     - Add PGP and DKIM support. What about AGE? And S/MIME?

    """
    NAME = 'email'
    ROLES = AccessConfig.GRANT_READ
    WEBSOCKET = False
    WEB_EXPOSE = True
    CONNECT = False    # We manually connect if we need to!
    OPTIONS = [[
        (None, None, 'moggie'),
        ('--context=', ['default'], 'Context to use for default settings'),
        ('--format=',   ['rfc822'], 'X=(rfc822*|text|json|sexp)'),
        ('--send-to=',          [], 'Address(es) to send to (igores headers)'),
        ('--send-at=',          [], 'X=(NOW|+seconds|a Unix timestamp)'),
        ('--send-via=',         [], 'X=(smtp|smtps)://[user:pass@]host:port'),
        ('--stdin=',            [], None), # Allow lots to send stdin (internal)
    ],[
        (None, None, 'headers'),
        ('--from=',      [],  'name <e-mail> OR account ID.'),
        ('--bcc=',       [],  'Hidden recipient (BCC)'),
        ('--to=',        [],  'To: recipient'),
        ('--cc=',        [],  'Cc: recipient'),
        ('--date=',      [],  'Message date, default is "now"'),
        ('--subject=',   [],  'Message subject'),
        ('--header=',    [],  'X="header:value", set arbitrary headers'),
    ],[
        (None, None, 'content'),
        ('--text=',      [],  'X=(N|"actual text content")'),
        ('--html=',      [],  'X=(N|"actual HTML content")'),
        ('--message=',   [],  'A snippet of text to add to the message'),
#FIXME: ('--template=',  [],  'Use a file or string as a message template'),
        ('--signature=', [],  'A snippet of text to append to the message'),
        ('--8bit',       [],  'Emit unencoded 8-bit text and HTML parts'),
        ('--attach=',    [],  'mimetype:/path/to/file'),
    ],[
        (None, None, 'searches'),
        ('--reply=',          [], 'Search terms, path to file or - for stdin'),
        ('--forward=',        [], 'Search terms, path to file or - for stdin'),
        ('--reply-to=',  ['all'], 'X=(all*|sender)'),
        ('--forwarding=',     [], 'X=(inline*|attachment|bounce)'),
        ('--quoting=',        [], 'X=(html*|text|trim*|below), many allowed'),
    ],[
        (None, None, 'encryption'),
        ('--sign-as=',      [], 'X=(N|auto|Key-ID), DKIM or PGP signing'),
        ('--decrypt=',      [], 'X=(N|auto|false|true)'),
        ('--encrypt=',      [], 'X=(N|all|attachments)'),
        ('--encrypt-to=',   [], 'X=(auto|Key-IDs), for PGP encryption'),
        ('--zip-password=', [], 'Password to use for ZIP encryption'),
    ]]

    DEFAULT_QUOTING = ['html', 'trim']
    DEFAULT_FORWARDING = ['html', 'inline']

    def __init__(self, *args, **kwargs):
        self.replying_to = []
        self.forwarding = []
        self.attachments = []
        self.headers = {}
        super().__init__(*args, **kwargs)

    def _load_email(self, fd):
        from moggie.email.parsemime import parse_message
        if fd == sys.stdin.buffer and self.options['--stdin=']:
            data = self.options['--stdin='].pop(0)
        else:
            data = fd.read()
        return parse_message(data, fix_mbox_from=(data[:5] == b'From '))

    def configure(self, args):
        args = self.strip_options(args)

        # FIXME: Accept the same JSON object as we emit; convert it back
        #        to command-line arguments here.
        # FIXME: Accept the same TEXT representation as we emit; convert it
        #        back to command-line arguments here.

        def as_file(key, i, t, target, reader):
            if t[:1] == '-':
                # FIXME: Is this how we handle stdin?
                target.append(reader(sys.stdin.buffer))
                self.options[key][i] = None
            elif (os.path.sep in t) and os.path.exists(t):
                with open(t, 'rb') as fd:
                    target.append(reader(fd))
                self.options[key][i] = None
            # FIXME: Allow in-place base64 encoded data?

        # This lets the caller provide messages for forwarding or replying to
        # directly, instead of searching. Anything left in the reply/forward
        # options after this will be treated as a search term.
        for target, key in (
                  (self.replying_to, '--reply='),
                  (self.forwarding,  '--forward=')):
            current = self.options.get(key, [])
            for i, t in enumerate(current):
                as_file(key, i, t, target, self._load_email)
            self.options[key] = [t for t in current if t]

        # Similarly, gather attachment data, if it is local. Anything left
        # in the attachment option will be treated as a remote reference.
        key = '--attach='
        current = self.options.get(key, [])
        for i, t in enumerate(current):
            if ':' in t:
                mt, path = t.split(':', 1)
            else:
                mt, path = 'application/octet-stream', t
            as_file(key, i, path, self.attachments,
                lambda fd: (mt, os.path.basename(path), fd.read()))
        self.options[key] = [t for t in current if t]

        # Complain if the user attempts both --text= and --message= style
        # composition; we want one or the other!
        if self.options.get('--message=') and (
                self.options['--text='] not in ([], ['N']) or
                self.options['--html='] not in ([], ['N'])):
            raise Nonsense('Use --message= or --text=/--html= (not both)')

        # Complain if the user tries to both compose a message and bounce
        # at the same time - bounces have already been fully composed.
        if 'bounce' in self.options.get('--forwarding='):
            if (len(self.forwarding) > 1
                    or len(self.options.get('--forward=')) > 1):
                raise Nonsense('Please only bounce/resend one message at a time.')
            for opt, val in self.options.items():
                if val and opt not in (
                        '--context=', '--format=',
                        '--forwarding=', '--forward=',
                        '--reply-to=',
                        '--from=', '--send-to=', '--send-at=', '--send-via='):
                    raise Nonsense('Bounced messages cannot be modified (%s%s)'
                        % (opt, val))
            if not self.options.get('--send-to='):
                raise Nonsense('Please specify --send-to= when bouncing')

        # Parse any supplied dates...
        import datetime
        key = '--date='
        current = self.options.get(key, [])
        for i, dv in enumerate(current):
            try:
                current[i] = datetime.datetime.fromtimestamp(int(dv))
            except ValueError:
                raise Nonsense('Dates must be Unix timestamps (FIXME)')

        # Parse and expand convert e-mail address options
        from moggie.email.addresses import AddressHeaderParser
        for opt in ('--from=', '--to=', '--cc=', '--bcc=', '--send-to='):
            if self.options[opt]:
                new_opt = []
                for val in self.options[opt]:
                    new_opt.extend(AddressHeaderParser(val))
                for val in (new_opt or [None]):
                    if not val or '@' not in (val.address or ''):
                        raise Nonsense('Failed to parse %s' % opt)
                self.options[opt] = new_opt

        return self.configure2(args)

    def _get_terms(self, args):
        """Used by Reply and Forward"""
        if '--' in args:
            pos = args.indexOf('--')
            if pos > 0:
                raise Nonsense('Unknown args: %s' % args[:pos])
            args = args[(pos+1):]
        else:
            opts = [a for a in args if a[:2] == '--']
            args = [a for a in args if a[:2] != '--']
        return args

    def configure2(self, args):
        if args:
            raise Nonsense('Unknown args: %s' % args)
        return args

    def text_part(self, text, mimetype='text/plain'):
        try:
            data = str(bytes(text, 'us-ascii'), 'us-ascii')
            enc = '7bit'
        except UnicodeEncodeError:
            if self.options['--8bit']:
                enc = '8bit'
                data = text
            else:
                import email.base64mime as b64
                data = b64.body_encode(bytes(text, 'utf-8'))
                enc = 'base64'
        return ({
                'content-type': [mimetype, ('charset', 'utf-8')],
                'content-disposition': 'inline',
                'content-transfer-encoding': enc
            }, data)

    def multi_part(self, mtype, parts):
        from moggie.email.headers import format_headers
        from moggie.util.mailpile import b64c, sha1b64
        import os
        boundary = b64c(sha1b64(os.urandom(32)))
        bounded = ['\r\n--%s\r\n%s%s' % (
                boundary,
                format_headers(headers),
                body
            ) for headers, body in parts]
        bounded.append('\r\n--%s--' % boundary)
        return ({
                'content-type': [
                    'multipart/%s' % mtype, ('boundary', boundary)],
                'content-transfer-encoding': '7bit'
            }, '\r\n'.join(bounded).strip())

    def attach_part(self, mimetype, filename, data):
        import email.base64mime as b64
        ctyp = [mimetype]
        disp = ['attachment']
        if filename:
            disp.append(('filename', filename))
        return ({
                'content-type': ctyp,
                'content-disposition': disp,
                'content-transfer-encoding': 'base64'
            }, b64.body_encode(data).strip())

    def get_encryptor(self):
        # FIXME: Implement this!
        return None, ''

    def get_passphrase(self):
        if self.options.get('--zip-password='):
            return bytes(self.options['--zip-password='][-1], 'utf-8')
        # FIXME: Generate a password? How do we tell the user?
        raise Nonsense('FIXME: need a password')
        return None

    def attach_encrypted_attachments(self, text_parts=None):
        from moggie.storage.exporters.maildir import ZipWriter
        import io, base64

        mimetype = 'application/octet-stream'
        filename = 'message.zip' if text_parts else 'attachments.zip'
        encryptor, ext = self.get_encryptor()

        passphrase = None
        if not encryptor or self.options.get('--zip-password'):
            passphrase = self.get_passphrase()
        if passphrase in (b'', b'NONE'):
            passphrase = None

        now = time.time()
        fd = io.BytesIO()
        zw = ZipWriter(fd, password=passphrase)
        if text_parts:
            for headers, b64data in text_parts:
                if headers['content-type'][0] == 'text/html':
                    fn = 'message.html'
                else:
                    fn = 'message.txt'
                zw.add_file(fn, now, base64.b64decode(b64data))
        for _unused, fn, data in self.attachments:
            zw.add_file(fn, now, data)
        zw.close()
        data = fd.getvalue()

        # If we are PGP or AGE encrypting the file, that transformation
        # happens here.
        if encryptor:
            filename += '.%s' % ext
            data = encryptor(data)

        return self.attach_part(mimetype, filename, data)

    def wrap_text(self, txt):
        lines = ['']
        for word in txt.replace('\r', '').replace('\n', ' ').split():
            if len(lines[-1]) + len(word) >= 72:
                lines.append('')
            lines[-1] += ' ' + word
        return '\r\n'.join(l.strip() for l in lines if l)

    def html_to_text(self, html):
        from moggie.security.html import html_to_markdown
        return html_to_markdown(html, wrap=72)

    def text_to_html(self, text):
        import markdown
        return markdown.markdown(text)

    def text_and_html(self, msg, is_html=None):
        msg = msg.strip()
        if is_html is True or (is_html is None and msg.startswith('<')):
            return self.html_to_text(msg), msg
        else:
            return msg, self.text_to_html(msg)

    def get_message_text(self, message, mimetype='text/plain'):
        if isinstance(message, MessagePart):
            message.with_text()
        found = []
        for part in message['_PARTS']:
            if part['content-type'][0] == mimetype and '_TEXT' in part:
                found.append(part['_TEXT'])
        return '\n'.join(found)

    def collect_quotations(self, message):
        import time
        import email.utils
        from moggie.security.html import HTMLCleaner

        #when = ' '.join(message['date'].strip().split()[:-2])
        if message['from']['fn']:
            frm = '%(fn)s <%(address)s>' % message['from']
        else:
            frm = message['from']['address']

        strategy = ','.join(self.options['--quoting='] or self.DEFAULT_QUOTING)
        quote_text = quote_html = ''

        def _quotebrackets(txt):
            return ''.join('> %s' % l for l in txt.strip().splitlines(True))
        quote_text = _quotebrackets(self.get_message_text(message))

        if 'html' in strategy or ('text' not in strategy and not quote_text):
            quote_html = self.get_message_text(message, mimetype='text/html')
            if quote_html:
                quote_html = '<blockquote>%s</blockquote>' % quote_html
                if 'text' not in strategy:
                    quote_text = None

        if quote_text and not quote_html:
            # Note: _quotebrackets becomes <blockquote>
            quote_html = self.text_to_html(quote_text)
        elif quote_html and not quote_text:
            quote_text = self.html_to_text(quote_html)

        if quote_text:
            if 'trim' in strategy and len(quote_text) > 1000:
                quote_text = (quote_text[:1000].rstrip()) + ' ...\n'
            quote_text = '%s wrote:\n%s' % (frm, quote_text)

        if quote_html:
            # FIXME: add our own CSS definitions, which the cleaner will then
            #        apply for prettification?
            quote_html = '<p>%s wrote:</p>\n%s' % (
                _html_quote(frm),
                HTMLCleaner(quote_html,
                    stop_after=(2000 if ('trim' in strategy) else None),
                    css_cleaner=CSSCleaner()).close())

        return strategy, quote_text, quote_html

    def collect_inline_forwards(self, message, count=0):
        strategy = ','.join(
            self.options['--forwarding='] or self.DEFAULT_FORWARDING)
        if 'inline' not in strategy:
            return strategy, '', ''

        fwd_text = self.get_message_text(message, mimetype='text/plain')
        fwd_html = ''
        if 'html' in strategy or ('text' not in strategy and not fwd_text):
            fwd_html = self.get_message_text(message, mimetype='text/html')
            if fwd_html and 'text' not in strategy:
                fwd_text = None
        if fwd_text and not fwd_html:
            fwd_html = self.text_to_html(fwd_text)
        elif fwd_html and not fwd_text:
            fwd_text = self.html_to_text(fwd_html)

        meta = []
        for hdr in ('Date', 'To', 'Cc', 'From', 'Subject'):
            vals = message.get(hdr.lower(), [])
            if not isinstance(vals, list):
                vals = [vals]
            if vals:
                if hdr in ('Date', 'Subject'):
                    meta.append((hdr, ' '.join(vals)))
                else:
                    def _fmt(ai):
                        if ai.get('fn'):
                            return '%(fn)s <%(address)s>' % ai
                        else:
                            return '<%(address)s>' % ai
                    meta.append((hdr, ', '.join(_fmt(v) for v in vals)))

        # FIXME: Is there a more elegant way to do this? Is this fine?
        fwd_text += '\n'
        fwd_html += '\n'
        for mt, filename, _ in self.forward_attachments(message, count=count):
            fwd_text += '\n[%s]' % filename
            fwd_html += '<p><tt>[%s]</tt></p>' % _html_quote(filename)

        fwd_text_meta = ("""\
-------- Original Message --------\n%s\n"""
            % ''.join('%s: %s\n' % (h, v) for h, v in meta))
        fwd_text = fwd_text_meta + (fwd_text or '(Empty message)\n')

        fwd_html_meta = ("""\
<p class="fwdMetainfo">-------- Original Message --------<br>\n%s</p>"""
                % ''.join('  <b>%s:</b> %s<br>\n' % (h, _html_quote(v))
                          for h, v in meta))
        fwd_html = '<div class="forwarded">\n%s\n</div>' % HTMLCleaner(
            fwd_html_meta + '\n\n' +
                (fwd_html or '<p><i>(Empty message)</i></p>'),
            css_cleaner=CSSCleaner()).close()

        return strategy, fwd_text, fwd_html

    def generate_text_parts(self, want_text, want_html):
        text, html = [], []

        quoting = {}
        for msg in self.options['--message=']:
            t, h = self.text_and_html(msg)
            text.append(t)
            html.append(self.wrap_text(h))

        for msg in self.replying_to:
            strategy, q_txt, q_htm = self.collect_quotations(msg)
            if q_txt and q_htm:
                if 'below' in strategy:
                    text[:0] = [q_txt]
                    html[:0] = [q_htm]
                else:
                    text.append(q_txt)
                    html.append(q_htm)

        for sig in self.options['--signature=']:
            t, h = self.text_and_html(sig)
            text.append('-- \r\n' + t)
            html.append('<br><br>--<br>\n' + self.wrap_text(h))

        for i, msg in enumerate(self.forwarding):
            strategy, f_txt, f_htm = self.collect_inline_forwards(msg, count=i)
            if f_txt and f_htm:
                text.append(f_txt)
                html.append(f_htm)

        if not want_text:
            text = []
        if not want_html:
            html = []
        return text, html

    def forward_attachments(self, msg, decode=base64.b64decode, count=0):
        strategy = ','.join(
            self.options['--forwarding='] or self.DEFAULT_FORWARDING)
        if decode in (None, False):
            decode = lambda d: None

        if 'inline' in strategy:
            for i, part in enumerate(msg['_PARTS']):
                mtyp, mattr = part.get('content-type', ['', {}])
                disp, dattr = part.get('content-disposition', ['', {}])
                if disp == 'attachment':
                    n = ('%d.%d-' % (count, i))
                    yield (
                        part['content-type'],
                        n + dattr.get('filename', mattr.get('name', 'att.bin')),
                        decode(part['_DATA']))

        elif 'attachment' in strategy:
            import datetime
            ts = datetime.datetime.fromtimestamp(msg.get('_DATE_TS', 0))
            subject = msg['from']['address']
            yield (
                'message/rfc822',
                '%4.4d%2.2d%2.2d-%2.2d%2.2d_%s_.eml' % (
                    ts.year, ts.month, ts.day, ts.hour, ts.minute, subject),
                decode(msg['_RAW']))

    def render(self):
        from moggie.email.headers import HEADER_CASEMAP, format_headers

        for hdr_val in self.options['--header=']:
            hdr, val = hdr_val.split(':', 1)
            if hdr.lower() in HEADER_CASEMAP:
                hdr = hdr.lower()
            h = self.headers[hdr] = self.headers.get(hdr, [])
            h.append(val)

        for hdr, opt in (
                ('from',    '--from='),
                ('to',      '--to='),
                ('cc',      '--cc='),
                ('date',    '--date='),
                ('subject', '--subject=')):
            h = self.headers[hdr] = self.headers.get(hdr, [])
            for v in self.options.get(opt, []):
                # Someone should spank me for playing golf
                (h.extend if isinstance(v, list) else h.append)(v)
            if not h:
                del self.headers[hdr]

        if 'date' not in self.headers:
            import datetime
            self.headers['date'] = [datetime.datetime.now()]

        if 'mime-version' not in self.headers:
            self.headers['mime-version'] = 1.0
        if 'message-id' not in self.headers:
            self.headers['message-id'] = _make_message_id()

        # Sanity checks
        if len(self.headers.get('from', [])) != 1:
            raise Nonsense('There must be exactly one From address!')
        if len(self.headers.get('date', [])) > 1:
            raise Nonsense('There can only be one Date!')

        msg_opt = self.options['--message=']
        text_opt = self.options['--text=']
        want_text = (msg_opt or text_opt) and (['N'] != text_opt)

        html_opt = self.options['--html=']
        want_html = (msg_opt or html_opt) and (['N'] != html_opt)

        if html_opt and 'Y' in text_opt:
            text_opt.append(self.html_to_text('\n\n'.join(html_opt)))

        elif text_opt and 'Y' in html_opt:
            html_opt.append(self.text_to_html('\n\n'.join(text_opt)))

        else:
            if not (want_html or want_text):
                want_html = True if not html_opt else False
                want_text = True if not text_opt else False
            text_opt, html_opt = self.generate_text_parts(want_text, want_html)

        # FIXME: Is this where we fork, on what the output format is?

        parts = []
        for i, msg in enumerate(self.forwarding):
            self.attachments.extend(self.forward_attachments(msg, count=i))

        text_opt = [t for t in text_opt if t not in ('', 'Y')]
        if want_text and text_opt:
            parts.append(self.text_part(
                '\r\n\r\n'.join(t.strip() for t in text_opt)))

        html_opt = [t for t in html_opt if t not in ('', 'Y')]
        if want_html and html_opt:
            parts.append(
                self.text_part(
                    '\r\n\r\n'.join(html_opt),
                    mimetype='text/html'))

        encryption = (self.options.get('--encrypt=') or ['N'])[-1].lower()
        if encryption == 'all' and not self.options['--encrypt-to=']:
            # Create an encrypted .ZIP with entire message content
            parts = [self.attach_encrypted_attachments(text_parts=parts)]
        else:
            if len(parts) > 1:
                parts = [self.multi_part('alternative', parts)]

            if encryption == 'attachments':
                # This will create an encrypted .ZIP with our attachments only
                parts.append(self.attach_encrypted_attachments())
            else:
                for mimetype, filename, data in self.attachments:
                    parts.append(self.attach_part(mimetype, filename, data))

        if len(parts) > 1:
            parts = [self.multi_part('mixed', parts)]

        if encryption == 'all' and self.options['--encrypt-to=']:
            # Encrypt to someone: a PGP or AGE key
            parts = [self.encrypt_to_recipient(parts)]

        if parts:
            self.headers.update(parts[0][0])
            body = parts[0][1]
        else:
            body = ''

        return ''.join([format_headers(self.headers), body])

    def _reply_addresses(self):
        senders = {}
        recipients = {}
        def _add(_hash, _ai):
             if not isinstance(_ai, AddressInfo):
                 _ai = AddressInfo(**_ai)
             _hash[_ai['address']] = _ai
        for email in self.replying_to:
             _add(senders, email['from'])
             for ai in email.get('to', []) + email.get('cc', []):
                 _add(recipients, ai)
        return senders, recipients

    def gather_subject(self):
        def _re(s):
            w1 = (s.split()[0] if s else '').lower()
            return s if (w1 == 're:') else 'Re: %s' % s
        def _fwd(s):
            w1 = (s.split()[0] if s else '').lower()
            return s if (w1 == 'fwd:') else 'Fwd: %s' % s
        
        subjects = []
        for msg in self.replying_to:
            subj = msg.get('subject')
            if subj:
                 subjects.append(_re(subj))
        for msg in self.forwarding:
            subj = msg.get('subject')
            if subj:
                 subjects.append(_fwd(subj))

        if subjects:
            subject = subjects[0]
            if len(subjects) > 1:
                subject += ' (+%d more)' % (len(subjects) - 1)
            self.options['--subject='] = [subject]

    def gather_from(self, senders_and_recipients=None):
        senders, recipients = senders_and_recipients or self._reply_addresses()

        # Check the current context for addresses that were on the
        # recipient list. If none are found, use the main address for
        # the context. If we are replying to ourself, prefer that!
        ctx = self.cfg.contexts[self.context]
        ids = self.cfg.identities
        for _id in (ids[i] for i in ctx.identities):
            if _id.address in senders:
                self.options['--from='] = [_id.as_address_info()]
                return 
        for _id in (ids[i] for i in ctx.identities):
            if _id.address in recipients:
                self.options['--from='] = [_id.as_address_info()]
                return

        # Default to our first identity (falls through if there are none)
        for _id in (ids[i] for i in ctx.identities):
            self.options['--from='] = [_id.as_address_info()]
            return 

        raise Nonsense('No from address, aborting')

    def gather_to_cc(self, senders_and_recipients=None):
        senders, recipients = senders_and_recipients or self._reply_addresses()

        frm = self.options['--from='][0].address

        self.options['--to='].extend(
            a.normalized() for a in senders.values() if a.address != frm)
        if self.options['--reply-to='][-1] == 'all':
            self.options['--cc='].extend(
                a.normalized() for a in recipients.values()
                if a.address != frm and a.address not in senders)

    async def gather_emails(self, searches, with_data=False):
        emails = []
        for search in searches:
            worker = self.connect()
            result = await self.worker.async_jmap(self.access,
                RequestSearch(context=self.context, terms=search))
            if result and 'emails' in result:
                for metadata in result['emails']:
                    msg = await self.worker.async_jmap(self.access,
                        RequestEmail(
                            metadata=metadata,
                            text=True,
                            data=with_data,
                            full_raw=with_data))
                    if msg and 'email' in msg:
                        emails.append(msg['email'])
        return emails

    async def gather_attachments(self, searches):
        atts = []
        for search in searches:
            raise Nonsense('FIXME: Searching for attachments does not yet work')
        return atts

    def render_result(self):
        self.print(self.render())

    def gather_recipients(self):
        recipients = []
        recipients.extend(self.options.get('--send-to=', []))
        if not recipients:
            recipients.extend(self.options.get('--to=', []))
            recipients.extend(self.options.get('--cc=', []))
            recipients.extend(self.options.get('--bcc=', []))
        return recipients

    async def do_send(self, render=None, recipients=None):
        recipients = recipients or self.gather_recipients()
        if render is None:
            render = self.render()

        via = (self.options.get('--send-via=') or [None])[-1]
        if not via:
            raise Nonsense('FIXME: Get via from config')
        
        transcript = []
        def _progress(happy, code, details, message):
            transcript.append((happy, code, details, message))
            return True

        from moggie.util.sendmail import sendmail
        frm = self.options['--from='][0].address
        await sendmail(render, [
                (via, frm, [r.address for r in recipients])
            ],
            progress_callback=_progress)

        self.print_json(transcript)

    async def do_bounce(self):
        recipients = self.gather_recipients()

        data = base64.b64decode(self.forwarding[0]['_RAW'])
        if data.startswith(b'From '):
            data = data[data.index(b'\n')+1:]

        eol = b'\r\n' if (b'\r\n' in data[:1024]) else b'\n'
        sep = eol + eol
        header, body = data.split(sep, 1)

        # According to https://www.rfc-editor.org/rfc/rfc5322.html#page-28
        # we are supposed to generate Resent-* headers and prepend to the
        # message header, with no other changes made. Nice and easy!
        from moggie.email.headers import format_headers
        resent_info = bytes(format_headers({
                'resent-to': recipients,
                'resent-message-id': _make_message_id(),
                'resent-from': self.options['--from='][0]},
            eol=str(eol, 'utf-8')), 'utf-8')[:-len(eol)]

        return await self.do_send(
             render=(resent_info + header + sep + body),
             recipients=recipients)

    async def run(self):
        for target, key, gather, args in (
                (self.replying_to, '--reply=',   self.gather_emails, []),
                (self.forwarding,  '--forward=', self.gather_emails, [True]),
                (self.attachments, '--attach=',  self.gather_attachments, [])):
            if self.options.get(key):
                target.extend(await gather(self.options[key], *args))

        if not self.options.get('--from='):
            self.gather_from()

        if 'bounce' in self.options.get('--forwarding='):
            if (len(self.forwarding) > 1
                    or len(self.options.get('--forward=')) > 1):
                raise Nonsense('Please only bounce/resend one message at a time.')

            await self.do_bounce()
        else:

            if not self.options.get('--to=') and not self.options.get('--cc='):
                self.gather_to_cc()

            if not self.options.get('--subject='):
                self.gather_subject()

            if self.options.get('--send-to=') or self.options('--send-at='): 
                await self.do_send()
            else:
                self.render_result()
