# This is a wrapper for invoking any tool which implements the
# Stateless OpenPGP Command-Line Interface, as defined in 
# https://datatracker.ietf.org/doc/html/draft-dkg-openpgp-stateless-cli-01
#
# We build on this in an attempt to minimize the lockin with any
# particular OpenPGP implementation.
#
import datetime
import threading

import sop

from typing import List, Optional, Dict, Sequence, MutableMapping
from typing import Tuple, BinaryIO, TYPE_CHECKING

from ..util.safe_popen import Unsafe_Popen, Safe_Popen, Safe_Pipe, PIPE


def background_run(method, *args):
    th = threading.Thread(target=method, args=args)
    th.daemon = True
    th.start()
    return th


def background_collect(method):
    sink = []
    def _collector():
        sink.append(method())
    th = background_run(_collector)
    return th, sink


def background_send(fd, data):
    data = bytes(data, 'utf-8') if isinstance(data, str) else data
    def _sender():
        fd.write(data)
        fd.close()
    return background_run(_sender)


def sop_date(ts):
    if not ts or (ts == '-'):
        return '-'
    if not isinstance(ts, datetime.datetime):
        ts = datetime.datetime.fromtimestamp(int(ts))
    return '%4.4d-%2.2d-%2.2dT%2.2d:%2.2d:%2.2dZ' % (
        ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second)


def sop_date_to_datetime(sd):
    if sd in (None, '-'):
        return None
    return datetime.datetime.strptime(sd, '%Y-%m-%dT%H:%M:%S%z')


class SOPError(IOError):
    def __init__(self, code, msg):
        super().__init__(str(msg, 'utf-8') if isinstance(msg, bytes) else msg)
        self.errno = code


class StatelessOpenPGPClient(sop.StatelessOpenPGP):
    """
    This implements the python-sop StatelessOpenPGP interface, shelling
    out to any SOP-compliant OpenPGP tool, using pipes and fork/exec to
    pass data, signatures and key material back and forth.

    This has quite a lot of overhead, but lets us change encryption
    backends quite easily. By adhering to the StatelessOpenPGP interface,
    we can should be able to avoid the overhead for any SOP implementation 
    written n Python, in particular the PGPy backend.
    """
    def __init__(self, binary):
        super().__init__(name='SopClient', version='0.0')
        self.binary = binary
        self.profiles = {}

    def make_pipe(self, we_read=False):
        pipe_obj = Safe_Pipe()
        fno = (pipe_obj.write_end if we_read else pipe_obj.read_end).fileno()
        return fno, pipe_obj

    def popen(self, *arguments, binary=None, keep_open=[]):
        command = [binary or self.binary] + list(arguments)
        #print('RUNNING: %s, keep_open=%s' % (command, keep_open))
        return Safe_Popen(command,
            stdin=PIPE, stdout=PIPE, stderr=PIPE,
            keep_open=keep_open)

    def run(self, *arguments, input_data=b'', keep_open=[], timeout=60):
        try:
            if isinstance(input_data, str):
                input_data = bytes(input_data, 'utf-8')
            child = self.popen(*arguments, keep_open=list(keep_open))
            so, se = child.communicate(input=input_data, timeout=timeout)
            #print('RETURNED: %s / %s / %s' % (child.returncode, so, se))
            return child.returncode, so, se
        except KeyboardInterrupt:
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            return -1, b'', bytes(traceback.format_exc(), 'utf-8')

    def list_profiles(self, subcommand='generate-key'):
        if self.profiles.get(subcommand) is None:
            rc, so, se = self.run('list-profiles', subcommand)
            if rc != 0:
                raise SOPError(rc, se)
            self.profiles[subcommand] = dict(
                l.split(': ', 1) for l in so.strip().splitlines())
        return self.profiles.get(subcommand, {})

    def generate_key(self,
            armor:bool=True,
            uids:List[str]=[],
            keypasswd:Optional[bytes]=None,
            profile:Optional[bytes]=None
            ) -> bytes:
        """
        Return a new (ascii armored) public/private keypair, generated
        using the given user IDs and profile. If a password is provided,
        the secret key material will be encrypted using the password.
        """
        assert(armor)
        if profile and profile not in self.list_profiles('generate-key'):
            raise SOPError(-1, 'Unknown SOP profile: %s' % profile)

        pipes = {}
        args = []
        if keypasswd:
            args.append('--with-key-password=@FD:0' % pipe_n)
        if profile:
            args.append('--profile=%s' % profile)
        if uids:
            args.append('--')
            args.extend(uids)

        rc, so, se = self.run('generate-key', *args, input_data=keypasswd)
              #, input_data=password)
        if rc != 0:
            raise SOPError(rc, se)

        return so

    def extract_cert(self, key:bytes, armor:bool=True, **kwargs) -> bytes:
        """
        Return the (ascii armored) sharable public key part from a keypair
        as generated by the `generate_key` method.
        """
        assert(armor)
        rc, so, se = self.run('extract-cert', input_data=key)
        if rc != 0:
            raise SOPError(rc, se)
        return so

    def sign(self,
            data:bytes,
            armor:bool=True,
            sigtype:sop.SOPSigType=sop.SOPSigType.binary,
            signers:MutableMapping[str,bytes]={},
            wantmicalg:bool=False,
            keypasswords:MutableMapping[str,bytes]={}
            ) -> Tuple[bytes, Optional[str]]:
        """
        Sign the data using the provided keys. Returns (micalg, signature).
        Input key material and the outputted signature should/will be ascii
        armored.
        """
        assert(armor)
        assert(len(keypasswords) == 0)
        assert(sigtype == sop.SOPSigType.binary)

        args = []
        keep_open = []
        micalg = alg_pipe = alg_th = None
        if wantmicalg:
            alg_fd, alg_pipe = self.make_pipe(we_read=True)
            alg_th, micalg = background_collect(alg_pipe.read)
            keep_open.append(alg_fd)
            args.append('--micalg-out=@FD:%d' % alg_fd)

        key_pipes = []
        args.append('--') 
        for key in signers.values():
            key_fd, key_pipe = self.make_pipe()
            keep_open.append(key_fd)
            key_pipes.append(key_pipe)  # Avoid garbage collection
            background_send(key_pipe.write_end, key)
            args.append('@FD:%d' % key_fd)

        rc, so, se = self.run('sign', *args,
            input_data=data,
            keep_open=keep_open)
        if rc != 0:
            raise SOPError(rc, se)

        try:
            if alg_pipe is not None:
                alg_pipe.write_end.close()
                alg_th.join()
                return (so, str(micalg[0], 'utf-8'))
            else:
                return (so, None)
        except IndexError as e:
            se = bytes(str(e), 'utf-8')
        raise SOPError(rc, so+se)

    def parse_verifications(self, verifications):
        def _parse(vinfo):
            ts, signing_key, signing_pkey, details = vinfo.split(None, 3) 
            return sop.SOPSigResult(
                when=sop_date_to_datetime(ts),
                signing_fpr=signing_key,
                primary_fpr=signing_pkey,
                moreinfo=details)

        return [_parse(l) for l in str(verifications, 'utf-8').splitlines()]

    def verify(self,
            data:bytes,
            start:Optional[datetime.datetime]=None,
            end:Optional[datetime.datetime]=None,
            sig:bytes=b'',
            signers:MutableMapping[str,bytes]={},
            ) -> List[sop.SOPSigResult]:
        """
        Returns (bool, details) explaining whether the given signatures
        and certificates match the, data and the signatures fall within the
        window of time defined by the not_before and not_after parameters
        (if specified).
        """
        keep_open = []
        sac_pipes = []
        sac_args = []
        for soc in [sig] + list(signers.values()):
            soc_fd, soc_pipe = self.make_pipe()
            keep_open.append(soc_fd)
            sac_pipes.append(soc_pipe)  # Avoid garbage collection
            background_send(soc_pipe.write_end, soc)
            sac_args.append('@FD:%d' % soc_fd)

        rc, so, se = self.run('verify',
            '--not-before=%s' % sop_date(start),
            '--not-after=%s' % sop_date(end),
            '--', *sac_args,
            input_data=data,
            keep_open=keep_open)
        if rc != 0:
            raise sop.SOPNoSignature()

        return self.parse_verifications(so)

    def encrypt(self,
            data:bytes,
            literaltype:sop.SOPLiteralDataType=sop.SOPLiteralDataType.binary,
            armor:bool=True,
            passwords:MutableMapping[str,bytes]={},
            signers:MutableMapping[str,bytes]={},
            keypasswords:MutableMapping[str,bytes]={},
            recipients:MutableMapping[str,bytes]={},
            ) -> bytes:
        assert(armor)
        assert(literaltype == sop.SOPLiteralDataType.binary)

        args = []
        pipes = []
        keep_open = []

        for arg, values in (
                ('with-password', passwords),
                ('sign-with', signers),
                ('sign-password', keypasswords),
                (None, recipients)):
            if not arg:
                args.append('--')
            for val in values.values():
                v_fd, v_pipe = self.make_pipe()
                background_send(v_pipe.write_end, val)
                keep_open.append(v_fd)
                pipes.append(v_pipe)
                if arg:
                    args.append('--%s=@FD:%d' % (arg, v_fd))
                else:
                    args.append('@FD:%d' % (v_fd,))

        rc, so, se = self.run('encrypt', '--as=binary', *args,
            input_data=data, keep_open=keep_open)
        if rc:
            raise SOPError(rc, se)

        return so

    def decrypt(self,
            data:bytes,
            wantsessionkey:bool=False,
            sessionkeys:MutableMapping[str, sop.SOPSessionKey]={},
            passwords:MutableMapping[str,bytes]={},
            signers:MutableMapping[str,bytes]={},
            start:Optional[datetime.datetime]=None,
            end:Optional[datetime.datetime]=None,
            keypasswords:MutableMapping[str,bytes]={},
            secretkeys:MutableMapping[str,bytes]={},
            ) -> Tuple[bytes,
                       List[sop.SOPSigResult],
                       Optional[sop.SOPSessionKey]]:
        assert(not wantsessionkey)  # FIXME

        args = []
        pipes = []
        keep_open = []

        if signers:
            ver_fd, ver_pipe = self.make_pipe(we_read=True)
            ver_th, verifications = background_collect(ver_pipe.read)
            keep_open.append(ver_fd)
            pipes.append(ver_pipe)
            args.append('--verifications-out=@FD:%d' % ver_fd)
            if start:
                args.append('--verify-not-before=%s' % sop_date(start))
            if end:
                args.append('--verify-not-after=%s' % sop_date(end))
        else:
            ver_th = verifications = None

        for arg, values in (
                ('with-password', passwords),
                ('with-session-key', sessionkeys),
                ('with-key-password', keypasswords),
                ('verify-with', signers),
                (None, secretkeys)):
            if not arg:
                args.append('--')
            for val in values.values():
                v_fd, v_pipe = self.make_pipe()
                background_send(v_pipe.write_end, val)
                keep_open.append(v_fd)
                pipes.append(v_pipe)
                if arg:
                    args.append('--%s=@FD:%d' % (arg, v_fd))
                else:
                    args.append('@FD:%d' % (v_fd,))

        rc, so, se = self.run('decrypt', *args,
            input_data=data, keep_open=keep_open)
        if rc:
            raise SOPError(rc, se)

        result = [so, None, None]

        if ver_th is not None:
            ver_pipe.write_end.close()
            ver_th.join()
            result[1] = self.parse_verifications(verifications[0])

        return tuple(result)


if __name__ == '__main__':
    import os
    moggie_root = os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', '..'))

    SOPC = StatelessOpenPGPClient

    assert(sop_date(0) == '-')
    assert(sop_date(1) == '1970-01-01T00:00:01Z')

    assert(SOPC('/bin/false').run('hello') == (1, b'', b''))
    assert(SOPC('/bin/true').run('hello') == (0, b'', b''))
    assert(SOPC('/nib/eurt').run('hello')[0] != 0)

    sopgpy = os.path.join(moggie_root, 'tools', 'sopgpy')
    try:
        from unused_sopgpy import SOPGPy
        sopc, which = SOPGPy(), 'sopgpy-inline'
    except:
        sopc = None
 
    if sopc:
        pass

    elif SOPC(sopgpy).run('version')[0] == 0:
        sopc, which = SOPC(sopgpy), 'sopgpy'

    elif SOPC('/usr/bin/sqop').run('version')[0] == 0:
        sopc, which = SOPC('/usr/bin/sqop'), 'sqop'

    else:
        sopc = which = None

    if sopc and which:
        skey = sopc.generate_key(uids=['Bjarni <bre@example.org>'])
        assert(skey.startswith(b'-----BEGIN PGP PRIVATE'))

        pkey = sopc.extract_cert(key=skey)
        assert(pkey.startswith(b'-----BEGIN PGP PUBLIC'))

        signature1, micalg = sopc.sign(
            data='hello world', signers={'':skey}, wantmicalg=False)
        assert(signature1.startswith(b'-----BEGIN PGP SIG'))

        signature2, micalg = sopc.sign(
            data='hello world', signers={'':skey}, wantmicalg=True)
        assert(micalg[:4] == 'pgp-')
        assert(signature2.startswith(b'-----BEGIN PGP SIG'))

        assert(sopc.verify(
            data='hello world', sig=signature1, signers={'':pkey})[0])
        try:
            sopc.verify(
                data='hello planet', sig=signature1, signers={'':pkey})
            assert(not 'reached')
        except sop.SOPNoSignature:
            pass

        encrypted = sopc.encrypt(
            data='hello world',
            recipients={'':pkey},
            signers={'':skey})
        assert(encrypted.startswith(b'-----BEGIN PGP MESS'))

        cleartext, verifications, sessionkeys = sopc.decrypt(
            data=encrypted, secretkeys={'':skey}, signers={'':pkey})
        assert(b'hello world' == cleartext)
        assert(len(verifications) == 1)

        print('Tests passed OK (using %s)' % which)

        #from moggie.crypto.openpgp_keyinfo import get_keyinfo
        #print('%s' % get_keyinfo(pkey))

    else:
        print('Tests passed OK')
