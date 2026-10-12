#!/usr/bin/env python3
"""Renewal hook: validate local PEM inputs, patch pre-provisioned TLS Secrets.

No private material is written to disk, printed, or passed in process arguments.
The explicit kubeconfig/context prevents accidentally modifying another cluster.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def run(argv, data=None):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=45)
    if result.returncode:
        # kubectl/openssl diagnostics can contain input material; do not echo them.
        raise ValueError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout


def protected_file(path):
    mode = path.stat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.geteuid():
        raise ValueError(f"{path}: must be a regular file owned by the hook user")
    if mode.st_mode & 0o077:
        raise ValueError(f"{path}: group/other permissions must be disabled (chmod 600)")


def validate(cert, key, hostnames, openssl):
    protected_file(key)
    run([openssl, 'x509', '-in', str(cert), '-noout', '-checkend', '86400'])
    san = run([openssl, 'x509', '-in', str(cert), '-noout', '-ext', 'subjectAltName'])
    if b'DNS:' not in san:
        raise ValueError('certificate must have DNS subjectAltName entries')
    for hostname in hostnames:
        result = run([openssl, 'x509', '-in', str(cert), '-noout', '-checkhost', hostname])
        if b'does match certificate' not in result:
            raise ValueError(f"certificate does not cover {hostname}")
    public_cert = run([openssl, 'x509', '-in', str(cert), '-noout', '-pubkey'])
    public_key = run([openssl, 'pkey', '-in', str(key), '-pubout', '-passin', 'pass:'])
    if public_cert != public_key:
        raise ValueError('certificate/private key mismatch')
    if cert.read_bytes().count(b'-----BEGIN CERTIFICATE-----') < 2:
        raise ValueError('full chain required (leaf followed by intermediate certificate(s))')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fullchain', 'key', 'kubeconfig', 'context'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--namespace', action='append', required=True)
    parser.add_argument('--hostname', action='append', required=True)
    parser.add_argument('--secret', default='lan-apps-tls')
    parser.add_argument('--kubectl', default='kubectl')
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--check-only', action='store_true', help='validate without any API calls')
    args = parser.parse_args()
    try:
        for name in [args.secret, *args.namespace]:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', name) or len(name) > 253:
                raise ValueError('invalid Secret/namespace name')
        cert, key, kubeconfig = map(Path, (args.fullchain, args.key, args.kubeconfig))
        if not all(path.is_absolute() for path in (cert, key, kubeconfig)):
            raise ValueError('input file paths must be absolute')
        protected_file(kubeconfig)
        validate(cert, key, args.hostname, args.openssl)
        if args.check_only:
            print('Certificate inputs validated; no API calls made.')
            return
        patch = json.dumps({'type': 'kubernetes.io/tls', 'data': {
            'tls.crt': base64.b64encode(cert.read_bytes()).decode(),
            'tls.key': base64.b64encode(key.read_bytes()).decode(),
        }}).encode()
        for namespace in dict.fromkeys(args.namespace):
            run([args.kubectl, '--kubeconfig', str(kubeconfig), '--context', args.context,
                 '--request-timeout=30s', '--namespace', namespace, 'patch', 'secret',
                 args.secret, '--type=merge', '--patch-file=/dev/stdin'], patch)
            print(f'Updated {namespace}/{args.secret}')
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f'Certificate sync failed: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
