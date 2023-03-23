# Low level commands exposing Moggie's OpenPGP support
#
import io
import logging
import os
import sys
import time

from .command import Nonsense, CLICommand, AccessConfig


class WorkerEncryptionWrapper:
    def __init__(self, cli_obj):
        pass


class CommandOpenPGP(CLICommand):
    """moggie pgp [<command>] [<options> ...]

    Low level commands for interacting with PGP keys, encrypting or
    decrypting.

    ## General options

    These options control the high-level behavior of this command; where
    it loads default settings from, what it does with the message once
    it has been generated, and how the output is formatted:

    %(moggie)s

    ### Examples:

        ...

    ## Known bugs and limitations

    A bunch of the stuff above isn't yet implemented. It's a lot!
    This is a work in progress.

    """
    NAME = 'pgp'
    ROLES = AccessConfig.GRANT_READ
    CONNECT = False    # We manually connect if we need to!
    WEBSOCKET = False
    WEB_EXPOSE = True
    OPTIONS_COMMON = [
        (None, None, 'moggie'),
        ('--context=', ['default'], 'Context to use for default settings'),
        ('--format=',   ['rfc822'], 'X=(rfc822*|text|json|sexp)'),
        ('--stdin=',            [], None), # Allow lots to send stdin (internal)
    ]
    OPTIONS_PGP_SETTINGS = [
        ('--pgp-sop=',          [], '"PGPy" or /path/to/SOP/tool'),
        ('--pgp-key-sources=',  [], 'Ordered list of key stores/sources'),
        ('--pgp-password=',     [], 'Password to use to unlock PGP keys'),
    ]
    OPTIONS_SIGNING = [
        (None, None, 'signing'),
        ('--sign-with=',        [], 'Keys or fingerprints to sign with'),
    ]
    OPTIONS_VERIFYING = [
        (None, None, 'verifying'),
        ('--verify-from=',     [], 'Keys or fingerprints to verify with'),
    ]
    OPTIONS_ENCRYPTING = [
        (None, None, 'encrypting'),
        ('--encrypt-to=',       [], 'Keys or fingerprints to encrypt to'),
    ]
    OPTIONS_DECRYPTING = [
        (None, None, 'decrypting'),
        ('--decrypt-with=',     [], 'Keys or fingerprints to decrypt with'),
    ]
    OPTIONS = [OPTIONS_COMMON + OPTIONS_PGP_SETTINGS]

    def __init__(self, *args, **kwargs):
        self.sign_with = []
        self.verify_from = []
        self.encrypt_to = []
        self.decrypt_with = []
        super().__init__(*args, **kwargs)

    @classmethod
    def configure_passwords(cls, cli_obj, which=['--pgp-password=']):
        for opt in which:
            for i, v in enumerate(cli_obj.options[opt]):
                if v and v.lower() == 'ask':
                    import getpass
                    prompt = 'Password (%s): ' % opt[2:-1]
                    cli_obj.options[opt][i] = getpass.getpass(prompt)

    @classmethod
    def configure_keys(cls, cli_obj):
        for arg in (
                '--sign-with=',
                '--verify-from=',
                '--encrypt-to=',
                '--decrypt-with='):
            keep = []
            for i, v in enumerate(cli_obj.options.get(arg) or []):
                v = v.strip()
                if v.startswith('PGP:'):
                    prefix = v[:4]
                    v = v[4:]
                else:
                    prefix = ''

                if v.startswith('-----BEGIN'):
                    pass
                elif v[:1] in ('.', '/'):
                    try:
                        with open(v, 'r') as fd:
                            key = fd.read().strip()
                            if not key.startswith('-----BEGIN'):
                                raise Nonsense(
                                    'Not an ASCII-armored OpenPGP key: %s' % v)
                            cli_obj.options[arg][i] = prefix + key
                    except (OSError, IOError):
                        raise Nonsense('File not found or unreadable: %s' % v)

    @classmethod
    def get_signing_ids_and_keys(cls, cli_obj):
        ids = {'DKIM': [], 'PGP': []}
        for _id in cli_obj.options['--sign-with=']:
            t, i = _id.split(':', 1)
            ids[t.upper()].append(i)
        return ids

    @classmethod
    def get_encryptor(cls, cli_obj):
        rcpt_ids = {'PGP': []}
        for _id in cli_obj.options['--encrypt-to=']:
            t, i = _id.split(':', 1)
            rcpt_ids[t.upper()].append(i)

        if not rcpt_ids['PGP']:
            return None, '', ''

        pgp_signing_ids = cls.get_signing_ids_and_keys(cli_obj)['PGP']
        sopc, keys = CommandOpenPGP.get_async_sop_and_keystore(cli_obj)

        async def encryptor(data):
            encrypt_args = {
                'data': bytes(data, 'utf-8'),
                'recipients': dict(enumerate(rcpt_ids['PGP']))}
            if pgp_signing_ids:
                encrypt_args['signers'] = dict(enumerate(pgp_signing_ids))
                if cli_obj.options['--pgp-password=']:
                    encrypt_args['keypasswords'] = dict(
                        enumerate(cli_obj.options['--pgp-password=']))
            return str(await sopc.encrypt(**encrypt_args), 'utf-8')

        return encryptor, 'OpenPGP', 'asc', 'application/pgp-encrypted'

    @classmethod
    def get_async_sop_and_keystore(cls, cli_obj):
        sop_cfg = (cli_obj.options.get('--pgp-sop=') or [None])[-1]
        keys_cfg = (cli_obj.options.get('--pgp-key-sources=') or [None])[-1]
        if sop_cfg or keys_cfg:
            from moggie.crypto.openpgp.sop import DEFAULT_SOP_CONFIG, GetSOPClient
            from moggie.crypto.openpgp.keystore import PrioritizedKeyStores
            from moggie.crypto.openpgp.keystore.registry import DEFAULT_KEYSTORES
            from moggie.crypto.openpgp.managers import CachingKeyManager
            from moggie.util.asyncio import AsyncProxyObject
            sc = GetSOPClient(sop_cfg or DEFAULT_SOP_CONFIG)
            ks = PrioritizedKeyStores(keys_cfg or DEFAULT_KEYSTORES)
            km = CachingKeyManager(sc, ks)
            return (
                AsyncProxyObject(sc, arg_filter=km.filter_key_args),
                AsyncProxyObject(ks))

        elif cli_obj.worker:
            we = WorkerEncryptionWrapper(worker)
            return we, we

        else:
            raise Nonsense('Need a backend worker or explicit PGP settings')

    @classmethod
    async def gather_pgp_keys(cls, cli_obj, terms, private_key=None):
        if False:
            yield None

    def configure(self, args):
        #self.preferences = self.cfg.get_preferences(context=self.context)
        args = self.strip_options(args)

        CommandOpenPGP.configure_keys(self)

        return self.configure2(args)

    def configure2(self, args):
        return args

    async def process_key_args(self):
        for arg, priv, target in (
                ('--sign-with=',    True, self.sign_with),
                ('--verify-from=', False, self.verify_from),
                ('--encrypt-to=',  False, self.encrypt_to),
                ('--decrypt-with=', True, self.decrypt_with)):
            for v in self.options.get(arg) or []:
                if v.startswith('PGP:-----BEGIN'):
                    target.append(v[4:])
                elif v.startswith('-----BEGIN'):
                    target.append(v)
                else:
                    async for key in CommandOpenPGP.gather_pgp_keys(
                            self, v, priv):
                        target.append(key)

    async def run(self):
        await self.process_key_args()

        self.print('FIXME %s' % self.decrypt_with)


class CommandPGPGetKeys(CommandOpenPGP):
    """moggie pgp-get-keys [<options>] <search-terms|fingerprint>

    %(OPTIONS)s
    """
    NAME = 'pgp-get-keys'


class CommandPGPAddKeys(CommandOpenPGP):
    """moggie pgp-add-keys [<options>] <ascii-armored-key>

    %(OPTIONS)s
    """
    NAME = 'pgp-add-keys'


class CommandPGPDelKeys(CommandOpenPGP):
    """moggie pgp-del-keys [<options>] <fingerprints>

    %(OPTIONS)s
    """
    NAME = 'pgp-del-keys'


class CommandPGPSign(CommandOpenPGP):
    """moggie pgp-sign [<options>]

    %(OPTIONS)s
    """
    NAME = 'pgp-sign'
    OPTIONS = ([
            CommandOpenPGP.OPTIONS_COMMON +
            CommandOpenPGP.OPTIONS_PGP_SETTINGS
        ]+[
            CommandOpenPGP.OPTIONS_SIGNING])


class CommandPGPEncrypt(CommandOpenPGP):
    """moggie pgp-encrypt [<options>] ...

    %(OPTIONS)s
    """
    NAME = 'pgp-encrypt'
    OPTIONS = ([
            CommandOpenPGP.OPTIONS_COMMON +
            CommandOpenPGP.OPTIONS_PGP_SETTINGS
        ]+[
            CommandOpenPGP.OPTIONS_SIGNING +
            CommandOpenPGP.OPTIONS_ENCRYPTING])


class CommandPGPDecrypt(CommandOpenPGP):
    """moggie pgp-decrypt [<options>]

    %(OPTIONS)s
    """
    NAME = 'pgp-decrypt'
    OPTIONS = ([
            CommandOpenPGP.OPTIONS_COMMON +
            CommandOpenPGP.OPTIONS_PGP_SETTINGS
        ]+[
            CommandOpenPGP.OPTIONS_VERIFYING +
            CommandOpenPGP.OPTIONS_DECRYPTING])
