import re
import hashlib

from html.parser import HTMLParser


class HTMLCleaner(HTMLParser):
    """
    This class will attempt to consume an HTML document and emit a new
    one which is roughly equivalent, but only has known-safe tags and
    attributes. The output is also guaranteed to not have nesting errors.

    It will strip (and count) potentially dangerous things like scripts
    and misleading links.

    It also generates keywords/fingerprints describing some technical
    features of the HTML, for use in the search engine and spam filters.

    FIXME:
       * Parse style sheets and convert them into style='' attributes.
       * Parse color and font size statements to prevent thing from being
         made invisible. Set a warning flag if we see this.
       * Callbacks when we see a references to attached images?
       * Do something smart when we see links?
    """
    ALLOW = lambda v: True
    RE_WEBSITE = re.compile('(https?:/+)?(([a-z0-9]+\.[a-z0-9]){2,}[a-z0-9]*)')
    CHECK_TARGET = re.compile('^(_blank)$').match
    CHECK_VALIGN = re.compile('^(top|bottom|center)$').match
    CHECK_HALIGN = re.compile('^(left|right|center)$').match
    CHECK_DIGIT = re.compile('^\d+$').match
    CHECK_SIZE = re.compile('^\d+(%|px)?$').match
    CHECK_LANG = re.compile('^[a-zA-Z-]+$').match
    CHECK_DIR = re.compile('^(ltr|rtl)$').match
    CHECK_CLASS = re.compile('^(mHtmlBody|mRemoteImage|mInlineImage|mso[a-z]+|wordsection\d+)$', re.IGNORECASE).match
    ALLOWED_ATTRIBUTES = {
        'alt':         ALLOW,
        'title':       ALLOW,
        'href':        ALLOW,  # FIXME
        'src':         ALLOW,  # FIXME
        'data-m-src':  ALLOW,  # We generate this to replace img src= attributes
        'name':        ALLOW,
        'target':      CHECK_TARGET,
        'bgcolor':     re.compile(r'^([a-zA-Z]+|\#[0-9a-f]+)$').match,
        'class':       CHECK_CLASS,
        'dir':         CHECK_DIR,
        'lang':        CHECK_LANG,
        'align':       CHECK_HALIGN,
        'valign':      CHECK_VALIGN,
        'width':       CHECK_SIZE,
        'height':      CHECK_SIZE,
        'border':      CHECK_DIGIT,
        'colspan':     CHECK_DIGIT,
        'rowspan':     CHECK_DIGIT,
        'cellspacing': CHECK_DIGIT,
        'cellpadding': CHECK_DIGIT}

    PROCESSED_TAGS = set([
        # We process these, so we can suppress them!
        'head', 'style', 'script',
        # These get rewritten to something else
        'body',
        # These are tags we pass through
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'br',
        'div', 'span', 'p', 'a', 'img',
        'table', 'thead', 'tbody', 'tr', 'th', 'td', 'ul', 'ol', 'li',
        'b', 'i', 'tt', 'center', 'strong', 'em', 'small', 'smaller', 'big'])

    SUPPRESSED_TAGS = set(['head', 'script', 'style', 'moggie_defanged'])
    DANGEROUS_TAGS = set(['script'])

    SINGLETON_TAGS = set(['hr', 'img', 'br'])
    CONTAINER_TAGS = set(['table', 'ul', 'ol', 'div'])
    SELF_NESTING = set([
        'div', 'span',
        'b', 'i', 'tt', 'center', 'strong', 'em', 'small', 'smaller', 'big'])

    def __init__(self, data=None, callbacks=None):
        super().__init__()
        self.cleaned = ''
        self.keywords = set([])
        self.tag_stack = []
        self.tags_seen = []
        self.dropped_tags = []
        self.dropped_attrs = []
        self.a_hrefs = []
        self.img_srcs = []

        self.builtins = {
            'body': lambda s,t,a,b: ('div', s._aa(a, 'class', 'mHtmlBody'), b),
            'a': self._clean_tag_a,
            'img': self._clean_tag_img}
        self.callbacks = callbacks or {}

        self.force_closed = 0
        self.saw_danger = 0

        if data:
            self.feed(data)

    def _aa(self, attrs, attr, value):
        """
        Append a value to an attribute, or set it. Used to add classes to tags.
        """
        for i, (a, v) in enumerate(attrs):
            if a == attr:
                attrs[i] = (a, v + ' ' + value)
                return attrs
        return attrs + [(attr, value)]

    def _parent_tags(self):
        return [t for t, a, b in self.tag_stack]

    def _container_tags(self, parent_tags=None):
        parent_tags = parent_tags or self._parent_tags()
        i = len(parent_tags)-1
        while (i > 0) and (parent_tags[i] not in self.CONTAINER_TAGS):
            i -= 1
        return parent_tags[i:]

    def _quote(self, t):
        return (
            t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

    def _quote_attr(self, t):
        return '"%s"' % self._quote(t).replace('"', '&quot;')

    def handle_decl(self, decl):
        self.tags_seen.append(decl)

    def handle_starttag(self, tag, attrs):
        # FIXME: Does this tag imply we open a container of some sort?
        #        Is that ever a thing?

        tag = tag.split(':', 1)[-1]   # FIXME: Handle namespaces better? No?
        if tag not in self.tags_seen:
            self.tags_seen.append(tag)
        if tag in self.DANGEROUS_TAGS:
            self.saw_danger += 1
        if tag not in self.PROCESSED_TAGS:
            self.dropped_tags.append(tag)
            return

        container = self._container_tags()

        # Does this tag imply we should close previous ones?
        if tag not in self.SELF_NESTING and tag in container:
            while self.tag_stack:
                closing = self.tag_stack[-1][0]
                if closing not in ('p', 'li'):
                    # Bare <p> and <li> are common enough to not count
                    self.force_closed += 1
                self.handle_endtag(closing)
                if closing == tag:
                    break

        # FIXME: Sanitize attributes
        self.tag_stack.append([tag, attrs, ''])
        if tag in self.SINGLETON_TAGS:
            self.handle_endtag(tag)

    def _clean_tag_a(self, _, t, attrs, b):
        """
        """
        def _tagless(c):
            return re.sub(r'<[^>]+>', '', c[:80])
        m = self.RE_WEBSITE.match(_tagless(b).lower())
        page_domain = m.group(2) if m else ''
        danger = []
        for i, (a, v) in enumerate(attrs):
            if (a == 'href') and v:
                self.a_hrefs.append(v)
                if m:
                    ok, parts = False, v.split('/')
                    for p in parts:
                        if p.endswith(page_domain):
                            ok = True
                    if not ok:
                        danger.append(i)
        if danger:
            for i in reversed(danger):
                a, v, = attrs.pop(i)
                self.dropped_attrs.append((t, a, v))
            self.saw_danger += 1
        return t, attrs, b

    def _clean_tag_img(self, _, t, attrs, b):
        remote = False
        inline = False
        for i, (a, v) in enumerate(attrs):
            if v and (a == 'src'):
                self.img_srcs.append(v)
                attrs[i] = ('data-m-src', v)
                inline = (v[:4] == 'cid:')
                remote = not inline
        if remote:
            return t, self._aa(attrs, 'class', 'mRemoteImage'), b
        elif inline:
            return t, self._aa(attrs, 'class', 'mInlineImage'), b
        else:
            return t, attrs, b

    def _clean_attributes(self, tag, attrs):
        for a, v in attrs:
            if a.startswith('on'):
                self.saw_danger += 1
            validator = self.ALLOWED_ATTRIBUTES.get(a)
            if validator and validator(v):
                yield a, v
            else:
                self.dropped_attrs.append((tag, a, v))

    def _render_attrs(self, attrs):
        return ''.join(' %s=%s' % (a, self._quote_attr(v))
            for a, v in attrs if (a and (v is not None)))

    def handle_endtag(self, tag):
        if not self.tag_stack:
            return

        tag = tag.split(':', 1)[-1]   # FIXME: Handle namespaces better? No?
        if tag != self.tag_stack[-1][0]:
            if tag in self._parent_tags()[:-1]:
                while tag != self.tag_stack[-1][0]:
                    self.force_closed += 1
                    self.handle_endtag(self.tag_stack[-1][0])

        if tag == self.tag_stack[-1][0]:
            t, a, b = self.tag_stack.pop(-1)
            for cbset in (self.builtins, self.callbacks):
                cb = cbset.get(t)
                if (cb is not None) and (t not in self.SUPPRESSED_TAGS):
                    t, a, b = cb(self, t, a, b)

            if t and t in self.SUPPRESSED_TAGS:
                self.dropped_tags.append(tag)
            elif t:
                a = self._render_attrs(self._clean_attributes(t, a))
                if t in self.SINGLETON_TAGS:
                    regenerated = '<%s%s>' % (t, a)
                else:
                    regenerated = '<%s%s>%s</%s>' % (t, a, b, t)
                if self.tag_stack:
                    self.tag_stack[-1][-1] += regenerated
                else:
                    self.cleaned += regenerated

    def handle_data(self, data):
        """
        Pass through any data parts, but ensure they are properly quoted.
        This should guarantee that anything not recognized as a tag by the
        HTMLParser won't be recognized as a tag downstream either.
        """
        if not data:
            return

        if self.tag_stack:
            self.tag_stack[-1][-1] += self._quote(data)
            t, a, _ = self.tag_stack[-1]
        else:
            self.cleaned += self._quote(data)
            t, a = None, None

        for cbset in (self.builtins, self.callbacks):
            cb = cbset.get('DATA')
            if (cb is not None) and (t not in self.SUPPRESSED_TAGS):
                cb(t, a, data)

    def close(self):
        super().close()
        # Close any dangling tags.
        while self.tag_stack:
            self.force_closed += 1
            self.handle_endtag(self.tag_stack[-1][0])
        self._make_html_keywords()
        self.cleaned = self.cleaned.strip()
        return self.cleaned

    def report(self):
        return """\
<!-- Made less spooky by moggie.security.html.HTMLCleaner

  * Spooky content: %d
  * Force-closed tags: %d
  * Keywords: %s
  * Link count: %d
  * Image count: %d
  * Encountered tags: %s
  * Dropped tags: %s
  * Dropped attributes: %s

-->"""  %  (self.saw_danger, self.force_closed,
            ', '.join(self.keywords),
            len(set(self.a_hrefs)),
            len(set(self.img_srcs)),
            ', '.join(self._quote(t) for t in self.tags_seen),
            ', '.join(self._quote(t) for t in self.dropped_tags),
            ''.join('\n    * (%s) %s=%s'
                % (da[0], self._quote(da[1] or ''), self._quote(da[2] or ''))
                for da in self.dropped_attrs))

    def _make_html_keywords(self):
        def _h16(stuff):
            return hashlib.md5(bytes(stuff, 'utf-8')).hexdigest()[:12]
        inline_images = len([i for i in self.img_srcs if i[:4] == 'cid:'])
        remote_images = len([i for i in self.img_srcs if i[:4] != 'cid:'])
        self.keywords.add(
            'html:code-%s' % ''.join([
                'd' if self.saw_danger else '',
                'f' if self.force_closed else '',
                'a' if self.dropped_attrs else '',
                'm' if (len(self.a_hrefs) + len(self.img_srcs)) > 10 else '',
                'l' if self.a_hrefs else '',
                'i' if self.img_srcs else '',
                'i' if inline_images else '']))
        self.keywords.add(
            'html:tags-%x-%s' % (
                len(self.dropped_tags),
                _h16(','.join(self.tags_seen))))
        if self.saw_danger:
            self.keywords.add('html:spooky')
        if self.img_srcs:
            self.keywords.add('html:images')
        if self.a_hrefs:
            self.keywords.add('html:links')
        if inline_images:
            self.keywords.add('html:inline-img')
        if remote_images:
            self.keywords.add('html:remote-img')

    def clean(self):
        self.close()
        return self.cleaned +'\n'+ self.report()


if __name__ == '__main__':
    import sys

    if sys.argv[1:] == ['-']:
        cleaner = HTMLCleaner(sys.stdin.read())
        print(cleaner.clean())
    else:
        input_data = """\
<!DOCTYPE html>
<html><head>
  <title>Hello world</title>
  <script>Very dangerous content</script>
</head><body>
  <p><h1>Hello <b>world < hello universe<p></h1>Para two<hr>
  <a href="http://spamsite/">https://<b>www.google.com</b>/</a>
  <a href="http://google.com.spamsite/">https://www.google.com/</a>
  <a href="https://www.google.com/">google.com</a>
  <a href="https://www.google.com/">www.google.com</a>
  <ul onclick="evil javascript;">
    <li>One
    <li>Two
    <li><ol><li>Three<li>Four</ol>
  </ul>
  <table>
    <tr><td>Hello<td>Lame<td>Table
  </table>
</body>"""

        def mk_kwe(kw):
            def kwe(tag, attrs, text):
                nonlocal kw
                if tag not in ('script', 'style'):
                     kw |= set(w.strip().lower()
                         for w in text.split(' ') if len(w) >= 3)
            return kwe

        keywords = set()
        cleaner = HTMLCleaner(input_data, callbacks={'DATA': mk_kwe(keywords)})
        cleaned = cleaner.close()
        cleaner.keywords |= keywords

        assert('DOCTYPE' not in cleaned)
        assert('<html'   not in cleaned)
        assert('<title'  not in cleaned)
        assert('<p></h1' not in cleaned)
        assert('</hr>'   not in cleaned)
        assert('onclick' not in cleaned)

        assert('<h1>Hello'        in cleaned)
        assert('world &lt; hello' in cleaned)

        assert('href'  not in cleaner.keywords)
        assert('body'  not in cleaner.keywords)

        assert('lame'           in cleaner.keywords)
        assert('hello'          in cleaner.keywords)
        assert('html:spooky'    in cleaner.keywords)
        assert('html:code-dfal' in cleaner.keywords)

        print(cleaned + cleaner.report())
