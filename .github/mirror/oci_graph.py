"""Walk and verify the full content graph of one image in a local OCI layout.

Used only by the one-time GHCR mirror workflows (ECR-RATE-1 option D).
Standard library only; reads local files; makes no network requests.

  python3 -I oci_graph.py LAYOUT_DIR PINNED_DIGEST MAX_BYTES [--expect FILE]

Narrow, fail-closed verifier for this mirror (not general OCI support):
- strict JSON: duplicate keys rejected;
- schemaVersion 2 and required typed fields for the index, every manifest
  and every descriptor; no nested indexes or subject associations;
- every blob's descriptor size is compared to the file size before hashing,
  and the cumulative byte bound is enforced before hashing;
- every edge is walked for every referencing platform, so the platform list
  of each node is complete even when a manifest is referenced twice;
- the single linux/amd64 manifest's config must declare os linux and
  architecture amd64.
Prints a JSON evidence document and exits non-zero on any inconsistency.
With --expect, the graph must equal the expected document exactly.
"""
import hashlib
import json
import os
import sys

INDEX_TYPES = {
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
}
MANIFEST_TYPES = {
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
}
CONFIG_TYPES = {
    'application/vnd.oci.image.config.v1+json',
    'application/vnd.docker.container.image.v1+json',
}
LAYER_TYPES = {
    'application/vnd.oci.image.layer.v1.tar+gzip',
    'application/vnd.oci.image.layer.v1.tar',
    'application/vnd.docker.image.rootfs.diff.tar.gzip',
    'application/vnd.in-toto+json',
}
MAX_DOC = 4 << 20  # bound on any JSON document that is parsed


class Fail(Exception):
    pass


def strict_object(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise Fail(f'duplicate JSON key {k!r}')
        out[k] = v
    return out


def parse(path):
    if os.path.getsize(path) > MAX_DOC:
        raise Fail(f'JSON document too large: {path}')
    with open(path, 'rb') as f:
        return json.loads(f.read(), object_pairs_hook=strict_object)


def need(cond, msg):
    if not cond:
        raise Fail(msg)


def descriptor(d, where):
    need(type(d) is dict, f'{where}: descriptor must be an object')
    need(type(d.get('mediaType')) is str, f'{where}: mediaType must be a string')
    need(type(d.get('digest')) is str, f'{where}: digest must be a string')
    need(type(d.get('size')) is int and d['size'] >= 0, f'{where}: size must be a non-negative int')
    return d['digest'], d['size'], d['mediaType']


class Graph:
    def __init__(self, layout, max_bytes):
        self.layout, self.max_bytes = layout, max_bytes
        self.nodes, self.total = {}, 0

    def blob_path(self, digest, size):
        algo, _, hexpart = digest.partition(':')
        need(algo == 'sha256' and len(hexpart) == 64 and all(c in '0123456789abcdef' for c in hexpart),
             f'unsupported digest {digest!r}')
        path = os.path.join(self.layout, 'blobs', 'sha256', hexpart)
        need(os.path.isfile(path), f'missing blob {digest}')
        actual = os.path.getsize(path)
        need(actual == size, f'size mismatch {digest}: descriptor {size}, file {actual}')
        return path

    def add(self, d, where, role, platform):
        digest, size, media = descriptor(d, where)
        path = self.blob_path(digest, size)
        prior = self.nodes.get(digest)
        if prior is not None:
            need((prior['size'], prior['mediaType'], prior['role']) == (size, media, role),
                 f'inconsistent reuse of {digest}')
            if platform not in prior['platforms']:
                prior['platforms'].append(platform)
                prior['platforms'].sort(key=str)
            return path
        need(self.total + size <= self.max_bytes, f'content exceeds bound {self.max_bytes}')
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        need('sha256:' + h.hexdigest() == digest, f'digest mismatch {digest}')
        self.total += size
        self.nodes[digest] = {'digest': digest, 'size': size, 'mediaType': media, 'role': role,
                              'platforms': [platform]}
        return path


def walk(layout, pinned, max_bytes):
    g = Graph(layout, max_bytes)
    top = parse(os.path.join(layout, 'index.json'))
    need(type(top.get('manifests')) is list, 'layout index.json: manifests must be a list')
    roots = [d for d in top['manifests'] if type(d) is dict and d.get('digest') == pinned]
    need(len(roots) == 1, 'layout must reference the pinned digest exactly once')
    root = roots[0]
    need(root.get('mediaType') in INDEX_TYPES, f'pinned digest is not an index: {root.get("mediaType")!r}')
    index = parse(g.add(root, 'root', 'index', None))
    need(index.get('schemaVersion') == 2 and type(index.get('schemaVersion')) is int, 'index: schemaVersion must be 2')
    need(index.get('mediaType', root['mediaType']) == root['mediaType'], 'index: mediaType disagrees with descriptor')
    need('subject' not in index, 'index: subject associations are not supported')
    children = index.get('manifests')
    need(type(children) is list and children, 'index: manifests must be a non-empty list')
    amd64 = []
    for i, m in enumerate(children):
        where = f'index.manifests[{i}]'
        descriptor(m, where)
        need(m['mediaType'] in MANIFEST_TYPES, f'{where}: unsupported mediaType {m["mediaType"]!r}')
        p = m.get('platform')
        need(type(p) is dict and type(p.get('os')) is str and type(p.get('architecture')) is str,
             f'{where}: platform os/architecture required')
        need('variant' not in p or type(p['variant']) is str, f'{where}: variant must be a string')
        platform = '/'.join(x for x in (p['os'], p['architecture'], p.get('variant')) if x)
        if p['os'] == 'linux' and p['architecture'] == 'amd64' and not p.get('variant'):
            amd64.append(m['digest'])
        man = parse(g.add(m, where, 'manifest', platform))
        need(man.get('schemaVersion') == 2 and type(man.get('schemaVersion')) is int, f'{where}: schemaVersion must be 2')
        need(man.get('mediaType', m['mediaType']) == m['mediaType'], f'{where}: mediaType disagrees with descriptor')
        need('subject' not in man, f'{where}: subject associations are not supported')
        cfg = man.get('config')
        descriptor(cfg, f'{where}.config')
        need(cfg['mediaType'] in CONFIG_TYPES, f'{where}.config: unsupported mediaType {cfg["mediaType"]!r}')
        g.add(cfg, f'{where}.config', 'config', platform)
        layers = man.get('layers')
        need(type(layers) is list and layers, f'{where}: layers must be a non-empty list')
        for j, layer in enumerate(layers):
            descriptor(layer, f'{where}.layers[{j}]')
            need(layer['mediaType'] in LAYER_TYPES, f'{where}.layers[{j}]: unsupported mediaType {layer["mediaType"]!r}')
            g.add(layer, f'{where}.layers[{j}]', 'layer', platform)
    need(len(amd64) == 1, f'expected exactly one linux/amd64 manifest, found {len(amd64)}')
    amd_man = parse(os.path.join(layout, 'blobs', 'sha256', amd64[0].split(':')[1]))
    amd_cfg = parse(os.path.join(layout, 'blobs', 'sha256', amd_man['config']['digest'].split(':')[1]))
    need(amd_cfg.get('os') == 'linux' and amd_cfg.get('architecture') == 'amd64',
         'linux/amd64 manifest config does not declare os linux / architecture amd64')
    present = set('sha256:' + f for f in os.listdir(os.path.join(layout, 'blobs', 'sha256')))
    extra = sorted(present - set(g.nodes))
    need(not extra, f'unreferenced blobs in layout: {extra[:5]}')
    return {'pinned': pinned, 'linux_amd64_manifest': amd64[0], 'blob_count': len(g.nodes),
            'total_bytes': g.total, 'nodes': [g.nodes[d] for d in sorted(g.nodes)]}


def main(argv):
    if len(argv) not in (4, 6) or (len(argv) == 6 and argv[4] != '--expect'):
        print(__doc__, file=sys.stderr)
        return 2
    layout, pinned = argv[1], argv[2]
    try:
        max_bytes = int(argv[3])
        graph = walk(layout, pinned, max_bytes)
        if len(argv) == 6:
            need(parse(argv[5]) == graph, 'graph differs from the expected document')
    except (Fail, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 1
    print(json.dumps(graph, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
