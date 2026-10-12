"""Offline LAN safety regressions; opt-in chart downloads never access a cluster."""
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import socket
import struct
import time
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

import yaml
from test_postgres_operator import APPLICATIONS, GITOPS, SERVICE, assert_schema, kustomize, load_yaml

INFRA = GITOPS / 'infrastructure'
CHARTS = {
    'metallb': ('0.16.1',
                'https://github.com/metallb/metallb/releases/download/metallb-chart-0.16.1/metallb-0.16.1.tgz',
                'fb06bb584fcb7856f15733b2a6a2aff5b61b5c350687e341c163ae24a5938adc'),
    'traefik': ('41.7.1',
                'https://traefik.github.io/charts/traefik/traefik-41.7.1.tgz',
                'f9edd5ca699ff0f5eab14df6e4feefa15f7597a9b56f0b1c01f6089b1eecbaf1'),
}


class LANTests(unittest.TestCase):
    def test_pools_are_singletons_and_only_selected_services_can_request_them(self):
        docs = kustomize(INFRA / 'metallb-config')
        pools = [d for d in docs if d['kind'] == 'IPAddressPool']
        self.assertEqual(len(pools), 2)
        self.assertEqual({p['metadata']['name']: p['spec']['addresses'] for p in pools}, {
            'lan-ingress': ['192.168.37.253/32'], 'lan-dns': ['192.168.37.254/32']})
        networks = [ipaddress.ip_network(p['spec']['addresses'][0]) for p in pools]
        self.assertFalse(networks[0].overlaps(networks[1]))
        for p in pools:
            self.assertIs(p['spec']['autoAssign'], False)
            allocation = p['spec']['serviceAllocation']
            self.assertEqual(len(allocation['namespaces']), 1)
            self.assertEqual(len(allocation['serviceSelectors']), 1)
            self.assertIn('app.kubernetes.io/name', allocation['serviceSelectors'][0]['matchLabels'])
        advertisement = next(d for d in docs if d['kind'] == 'L2Advertisement')
        self.assertEqual(set(advertisement['spec']['ipAddressPools']), {'lan-ingress', 'lan-dns'})

    def test_dns_is_separate_dual_protocol_nonroot_and_lan_only(self):
        docs = kustomize(INFRA / 'lan-dns')
        self.assertTrue(all(d['metadata'].get('namespace') == 'lan-dns'
                            for d in docs if d['kind'] != 'Namespace'))
        svc = next(d for d in docs if d['kind'] == 'Service')
        self.assertEqual(svc['spec']['type'], 'LoadBalancer')
        self.assertEqual(svc['metadata']['annotations']['metallb.io/loadBalancerIPs'], '192.168.37.254')
        self.assertEqual({(p['port'], p['protocol']) for p in svc['spec']['ports']}, {(53, 'UDP'), (53, 'TCP')})
        self.assertEqual(svc['spec']['loadBalancerSourceRanges'], ['192.168.0.0/16'])
        self.assertFalse(svc['spec']['allocateLoadBalancerNodePorts'])
        self.assertEqual(svc['spec']['externalTrafficPolicy'], 'Local')
        pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod.get('hostNetwork', False))
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['containers'][0]['image'], 'coredns/coredns:1.14.7')
        security = pod['containers'][0]['securityContext']
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertTrue(security['readOnlyRootFilesystem'])
        self.assertEqual(security['capabilities']['drop'], ['ALL'])
        self.assertEqual(security['capabilities']['add'], ['NET_BIND_SERVICE'])
        config = next(d for d in docs if d['kind'] == 'ConfigMap')
        self.assertEqual(pod['volumes'][0]['configMap']['name'], config['metadata']['name'])
        core = config['data']['Corefile']
        self.assertIn('hij1.local.sasakulab.com:1053', core)
        self.assertNotIn('fallthrough', core)
        self.assertNotIn('forward . /etc/resolv.conf', core)
        upstreams = next(line.split()[2:] for line in core.splitlines() if line.strip().startswith('forward .'))
        for upstream in upstreams:
            address = ipaddress.ip_address(upstream)
            self.assertNotIn(address, ipaddress.ip_network('192.168.37.252/30'))
            self.assertFalse(any(address in ipaddress.ip_network(cidr) for cidr in ['10.42.0.0/16', '10.43.0.0/16']))
        for name, ip in [('sakuta', '.110'), ('kaede', '.112'), ('nodoka', '.113')]:
            self.assertIn(f'{name} IN A 192.168.37{ip}', config['data']['hij1.zone'])
        for name in ['argocd', 'postgres']:
            self.assertIn(f'{name} IN A 192.168.37.253', config['data']['services.zone'])
            self.assertIn(f'{name}.local.sasakulab.com:1053', core)
        self.assertNotIn('*', config['data']['services.zone'])
        self.assertTrue(any(d['kind'] == 'NetworkPolicy' for d in docs))

    def test_routes_terminate_tls_and_postgres_is_not_active(self):
        active = kustomize(INFRA / 'lan-routes')
        routes = [d for d in active if d['kind'] == 'Ingress']
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route['metadata']['namespace'], 'argocd')
        self.assertEqual(route['spec']['ingressClassName'], 'lan-traefik')
        self.assertEqual(route['spec']['tls'], [{'hosts': ['argocd.local.sasakulab.com'], 'secretName': 'lan-apps-tls'}])
        backend = route['spec']['rules'][0]['http']['paths'][0]['backend']['service']
        self.assertEqual(backend, {'name': 'argocd-server', 'port': {'name': 'http'}})
        install = kustomize(GITOPS / 'argocd/install')
        params = next(d for d in install if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'argocd-cmd-params-cm')
        self.assertEqual(params['data']['server.insecure'], 'true')
        deployment = next(d for d in install if d['kind'] == 'Deployment' and d['metadata']['name'] == 'argocd-server')
        self.assertIn('lan.sasakulab.com/tls-termination', deployment['spec']['template']['metadata']['annotations'])
        optional = kustomize(INFRA / 'lan-routes/postgres')
        middleware = next(d for d in optional if d['kind'] == 'Middleware')
        ingress = next(d for d in optional if d['kind'] == 'Ingress')
        self.assertEqual(middleware['spec']['basicAuth']['secret'], 'postgres-ui-auth')
        self.assertEqual(ingress['metadata']['annotations']['traefik.ingress.kubernetes.io/router.middlewares'],
                         'postgres-operator-postgres-ui-auth@kubernetescrd')
        self.assertEqual(ingress['spec']['tls'][0]['secretName'], 'lan-apps-tls')
        self.assertTrue(all(d['metadata']['namespace'] == 'postgres-operator' for d in optional))
        self.assertFalse(any(d['kind'] == 'Secret' for d in active + optional))

    def test_certificate_sync_rbac_cannot_read_or_create_secrets(self):
        docs = kustomize(INFRA / 'lan-cert-sync')
        roles = [d for d in docs if d['kind'] == 'Role']
        self.assertEqual({d['metadata']['namespace'] for d in roles}, {'argocd', 'postgres-operator'})
        for role in roles:
            self.assertEqual(role['rules'], [{'apiGroups': [''], 'resources': ['secrets'],
                                             'resourceNames': ['lan-apps-tls'], 'verbs': ['patch']}])
        self.assertFalse(next(d for d in docs if d['kind'] == 'ServiceAccount')['automountServiceAccountToken'])
        self.assertFalse(any(d['kind'] == 'Secret' for d in docs))

    def test_application_pins_and_dependencies(self):
        apps = {d['metadata']['name']: d for d in kustomize(APPLICATIONS)}
        for app in apps.values():
            self.assertFalse(app['spec']['syncPolicy']['automated']['prune'])
        for name, (version, _, _) in CHARTS.items():
            self.assertEqual(apps['local-' + name]['spec']['source']['targetRevision'], version)
        self.assertLess(int(apps['local-metallb']['metadata']['annotations']['argocd.argoproj.io/sync-wave']),
                        int(apps['local-metallb-config']['metadata']['annotations']['argocd.argoproj.io/sync-wave']))
        values = apps['local-traefik']['spec']['source']['helm']['valuesObject']
        self.assertFalse(values['ingressClass']['isDefaultClass'])
        self.assertFalse(values['providers']['kubernetesCRD']['allowCrossNamespace'])
        self.assertNotIn('insecureSkipVerify', yaml.safe_dump(values))
        self.assertEqual(values['service']['annotations']['metallb.io/loadBalancerIPs'], '192.168.37.253')


@unittest.skipUnless(os.environ.get('LAN_TEST_CHARTS') == '1' and shutil.which('helm'),
                     'opt-in chart tests need LAN_TEST_CHARTS=1, Helm and network')
class ChartTests(unittest.TestCase):
    def test_verified_charts_render_with_committed_values_and_selectors(self):
        for name, (version, url, digest) in CHARTS.items():
            with self.subTest(chart=name), tempfile.TemporaryDirectory() as tmp:
                archive = urllib.request.urlopen(url, timeout=30).read()
                self.assertEqual(hashlib.sha256(archive).hexdigest(), digest)
                chart = Path(tmp) / (name + '.tgz')
                chart.write_bytes(archive)
                source = load_yaml(APPLICATIONS / (name + '.yaml'))['spec']['source']
                values = Path(tmp) / 'values.yaml'
                values.write_text(yaml.safe_dump(source['helm']['valuesObject']))
                ns = load_yaml(APPLICATIONS / (name + '.yaml'))['spec']['destination']['namespace']
                rendered = subprocess.check_output(['helm', 'template', name, str(chart), '--namespace', ns,
                    '--kube-version', '1.37.1', '--include-crds', '-f', str(values)], text=True)
                docs = [d for d in yaml.safe_load_all(rendered) if d]
                if name == 'traefik':
                    svc = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'lan-traefik')
                    pool = next(d for d in kustomize(INFRA / 'metallb-config') if d['metadata']['name'] == 'lan-ingress')
                    selector = pool['spec']['serviceAllocation']['serviceSelectors'][0]['matchLabels']
                    self.assertTrue(set(selector.items()) <= set(svc['metadata']['labels'].items()))
                    self.assertEqual(svc['spec']['loadBalancerSourceRanges'], ['192.168.0.0/16'])
                    self.assertEqual({p['port'] for p in svc['spec']['ports']}, {80, 443})
                    self.assertFalse(svc['spec']['allocateLoadBalancerNodePorts'])
                    deployment = next(d for d in docs if d['kind'] == 'Deployment')
                    args = deployment['spec']['template']['spec']['containers'][0]['args']
                    self.assertTrue(any('redirections.entryPoint.to=' in a for a in args))
                    self.assertTrue(any('ingressclass=lan-traefik' in a.lower() for a in args))
                else:
                    self.assertTrue(any(d['kind'] == 'CustomResourceDefinition' and
                        d['metadata']['name'] == 'ipaddresspools.metallb.io' for d in docs))
                    self.assertFalse(any('frr-k8s' in d['metadata']['name'] for d in docs))
                    for resource in kustomize(INFRA / 'metallb-config'):
                        crd = next(d for d in docs if d['kind'] == 'CustomResourceDefinition'
                                   and d['spec']['names']['kind'] == resource['kind'])
                        version = resource['apiVersion'].split('/')[1]
                        schema = next(v['schema']['openAPIV3Schema'] for v in crd['spec']['versions']
                                      if v['name'] == version)
                        assert_schema(self, resource['spec'], schema['properties']['spec'])


@unittest.skipUnless(os.environ.get('LAN_COREDNS_BINARY'), 'set LAN_COREDNS_BINARY for local DNS runtime checks')
class DNSRuntimeTests(unittest.TestCase):
    def test_committed_config_answers_authoritatively_over_udp_and_tcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['hij1.zone', 'services.zone']:
                shutil.copyfile(INFRA / 'lan-dns' / name, root / name)
            def free_port():
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    return sock.getsockname()[1]
            dns, health, ready = free_port(), free_port(), free_port()
            core = (INFRA / 'lan-dns/Corefile').read_text().replace('/etc/coredns', tmp)
            core = core.replace(':1053', ':' + str(dns)).replace(':8080', ':' + str(health)).replace(':8181', ':' + str(ready))
            (root / 'Corefile').write_text(core)
            proc = subprocess.Popen([os.environ['LAN_COREDNS_BINARY'], '-conf', str(root / 'Corefile')],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                for _ in range(50):
                    if proc.poll() is not None:
                        self.fail(proc.stdout.read().decode())
                    try:
                        urllib.request.urlopen(f'http://127.0.0.1:{ready}/ready', timeout=0.2).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail('CoreDNS did not become ready')
                for tcp in [False, True]:
                    for hostname, expected in [
                        ('sakuta.hij1.local.sasakulab.com', '192.168.37.110'),
                        ('kaede.hij1.local.sasakulab.com', '192.168.37.112'),
                        ('nodoka.hij1.local.sasakulab.com', '192.168.37.113'),
                        ('argocd.local.sasakulab.com', '192.168.37.253'),
                        ('postgres.local.sasakulab.com', '192.168.37.253'),
                        ('missing.hij1.local.sasakulab.com', None),
                    ]:
                        question = b''.join(bytes([len(label)]) + label.encode() for label in hostname.split('.')) + b'\x00'
                        packet = struct.pack('!6H', 123, 0x100, 1, 0, 0, 0) + question + struct.pack('!2H', 1, 1)
                        with socket.socket(type=socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
                            sock.settimeout(2)
                            sock.connect(('127.0.0.1', dns))
                            if tcp:
                                sock.sendall(struct.pack('!H', len(packet)) + packet)
                                data = b''
                                while len(data) < 2:
                                    data += sock.recv(2 - len(data))
                                length = struct.unpack('!H', data)[0]
                                response = b''
                                while len(response) < length:
                                    chunk = sock.recv(length - len(response))
                                    self.assertTrue(chunk)
                                    response += chunk
                            else:
                                sock.send(packet)
                                response = sock.recv(4096)
                        _, flags, _, answers, _, _ = struct.unpack('!6H', response[:12])
                        self.assertTrue(flags & 0x400, hostname)
                        self.assertEqual(flags & 0xf, 0 if expected else 3, hostname)
                        self.assertEqual(answers, 1 if expected else 0, hostname)
                        if expected:
                            offset = 12 + len(question) + 4
                            while response[offset]:
                                if response[offset] & 0xc0 == 0xc0:
                                    offset += 2
                                    break
                                offset += response[offset] + 1
                            else:
                                offset += 1
                            rr_type, rr_class, _, size = struct.unpack('!HHIH', response[offset:offset + 10])
                            self.assertEqual((rr_type, rr_class, size), (1, 1, 4))
                            self.assertEqual(socket.inet_ntoa(response[offset + 10:offset + 14]), expected)
            finally:
                proc.terminate()
                proc.communicate(timeout=5)


class CertificateHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = SERVICE / 'scripts/sync-lan-cert.py'
        spec = importlib.util.spec_from_file_location('cert_hook', path)
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required')
    def test_real_pem_hostname_key_match_and_chain_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key, cert, ca_key, ca = [root / name for name in ['key.pem', 'cert.pem', 'ca-key.pem', 'ca.pem']]
            def openssl(*args):
                subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True, timeout=30)
            openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', ca_key,
                    '-out', ca, '-days', '3', '-subj', '/CN=Test CA')
            openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
                    '-out', root / 'csr.pem', '-subj', '/CN=argocd.local.sasakulab.com')
            (root / 'ext').write_text('subjectAltName=DNS:argocd.local.sasakulab.com,DNS:postgres.local.sasakulab.com\n')
            openssl('x509', '-req', '-in', root / 'csr.pem', '-CA', ca, '-CAkey', ca_key,
                    '-CAcreateserial', '-out', cert, '-days', '2', '-extfile', root / 'ext')
            chain = root / 'chain.pem'
            chain.write_bytes(cert.read_bytes() + ca.read_bytes())
            key.chmod(0o600)
            ca_key.chmod(0o600)
            self.hook.validate(chain, key, ['argocd.local.sasakulab.com', 'postgres.local.sasakulab.com'], 'openssl')
            with self.assertRaises(ValueError):
                self.hook.validate(chain, key, ['sakuta.hij1.local.sasakulab.com'], 'openssl')
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                self.hook.validate(chain, ca_key, ['argocd.local.sasakulab.com'], 'openssl')
            with self.assertRaisesRegex(ValueError, 'full chain'):
                self.hook.validate(cert, key, ['argocd.local.sasakulab.com'], 'openssl')

    def test_subprocess_failure_does_not_echo_secret_material(self):
        result = subprocess.CompletedProcess(['kubectl'], 1, b'PRIVATE KEY', b'PRIVATE KEY')
        with patch.object(self.hook.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, r'kubectl failed \(exit 1\)') as error:
                self.hook.run(['kubectl'], b'PRIVATE KEY')
        self.assertNotIn('PRIVATE KEY', str(error.exception))

    def test_rejects_unprotected_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            key.write_text('not a real secret')
            key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'permissions'):
                self.hook.protected_file(key)
            key.chmod(0o600)
            self.hook.protected_file(key)

    def test_validation_precedes_api_and_failed_checks_never_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ['cert', 'key', 'kubeconfig']]
            for path in paths:
                path.write_text('test input')
                path.chmod(0o600)
            argv = ['sync', '--fullchain', str(paths[0]), '--key', str(paths[1]),
                    '--kubeconfig', str(paths[2]), '--context', 'explicit', '--namespace', 'argocd',
                    '--namespace', 'postgres-operator', '--hostname', 'argocd.local.sasakulab.com']
            with patch('sys.argv', argv), patch.object(self.hook, 'validate', side_effect=ValueError('invalid')), \
                 patch.object(self.hook, 'run') as run, patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    self.hook.main()
                run.assert_not_called()
            with patch('sys.argv', argv), patch.object(self.hook, 'validate'), \
                 patch.object(self.hook, 'run') as run, patch('sys.stdout', new_callable=io.StringIO):
                self.hook.main()
                self.assertEqual(run.call_count, 2)
                for call in run.call_args_list:
                    args, payload = call.args
                    self.assertIn('--patch-file=/dev/stdin', args)
                    self.assertIn('explicit', args)
                    self.assertNotIn('create', args)
                    self.assertEqual(json.loads(payload)['type'], 'kubernetes.io/tls')
