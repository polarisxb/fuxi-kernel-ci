#!/usr/bin/env python3
"""Offline verification of the locked fuxi baseline or SukiSU build.

Never accesses a device. A passing result is not a complete ABI or runtime claim.
"""
import argparse
import concurrent.futures
import functools
import fcntl
import os
import importlib.util
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

# Local helper imports must not mutate an exact, immutable delivery inventory.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
STOCK = ROOT / 'inputs/stock'
IDENTITY = json.loads((ROOT / 'configs/build-identity.json').read_bytes())
EXPECTED_SOURCE = IDENTITY['common_commit']
NM = ROOT / 'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/llvm-nm'
REQUIRED_CONFIG = {
    'CONFIG_LTO_CLANG_FULL': 'y', 'CONFIG_LTO_CLANG_THIN': 'n',
    'CONFIG_CFI_CLANG': 'y', 'CONFIG_CFI_PERMISSIVE': 'n',
    'CONFIG_MODVERSIONS': 'y', 'CONFIG_MODULE_SIG_PROTECT': 'y',
    'CONFIG_MODULE_SIG': 'y', 'CONFIG_MODULE_SIG_ALL': 'y',
    'CONFIG_MODULE_FORCE_LOAD': 'n',
    'CONFIG_KPM': 'n',
    'CONFIG_MODULE_ALLOW_MISSING_NAMESPACE_IMPORTS': 'n',
}


def command(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: '
                           f'{result.stderr.decode(errors="replace")[:2000]}')
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def config(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.startswith('CONFIG_'):
            key, value = line.split('=', 1)
            result[key] = value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            result[line.split()[1]] = 'n'
    return result


def symvers(path):
    result = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        fields = line.split()
        if len(fields) < 4:
            raise ValueError(f'Malformed Module.symvers line {number}')
        crc, symbol, provider, kind = fields[:4]
        namespace = fields[4] if len(fields) > 4 else ''
        record = {'crc': int(crc, 16), 'provider': provider, 'kind': kind, 'namespace': namespace}
        if symbol in result and result[symbol] != record:
            raise ValueError(f'Conflicting duplicate export {symbol}')
        result[symbol] = record
    return result



def read_elf64(path):
    """Validate table/file bounds before trusting any ELF header or symbols."""
    size=path.stat().st_size
    with path.open('rb') as stream:
        header=stream.read(64)
        if len(header)!=64 or header[:6]!=b'\x7fELF\x02\x01':
            raise ValueError(f'Not a little-endian ELF64 file: {path}')
        kind,machine,version,entry,phoff,shoff,flags,ehsize,phsize,phnum,shsize,shnum,shstr=struct.unpack_from('<HHIQQQIHHHHHH',header,16)
        if machine!=183 or version!=1 or ehsize!=64 or shsize!=64 or not shnum:
            raise ValueError(f'Invalid AArch64 ELF structure: {path}')
        if shoff<64 or shoff+shnum*shsize>size or shstr>=shnum:
            raise ValueError(f'Truncated ELF section table: {path}')
        if phnum and (phsize!=56 or phoff<64 or phoff+phnum*phsize>size):
            raise ValueError(f'Truncated ELF program header table: {path}')
        stream.seek(shoff)
        sections=[struct.unpack('<IIQQQQIIQQ',stream.read(64)) for _ in range(shnum)]
        for section in sections:
            if section[1]!=8 and section[4]+section[5]>size:
                raise ValueError(f'Truncated ELF section data: {path}')
        stream.seek(sections[shstr][4]);names=stream.read(sections[shstr][5])
        named={}
        for index,section in enumerate(sections):
            start=section[0]
            if start>=len(names):raise ValueError(f'Invalid ELF section name: {path}')
            name=names[start:names.find(b'\0',start)].decode(errors='replace')
            named[name]=(index,section)
        if phnum:
            stream.seek(phoff)
            for _ in range(phnum):
                typ,pflags,offset,vaddr,paddr,filesz,memsz,align=struct.unpack('<IIQQQQQQ',stream.read(56))
                if offset+filesz>size or filesz>memsz:raise ValueError(f'Truncated ELF load segment: {path}')
    if '.symtab' not in named or '.strtab' not in named:raise ValueError(f'ELF symbol tables missing: {path}')
    return {'kind':kind,'sections':sections,'named':named,'bytes':size}


@functools.lru_cache(maxsize=1024)
def module_export_namespaces(path):
    path=Path(path);elf=read_elf64(path)
    section=elf['named']['.symtab'][1]
    strings=elf['sections'][section[6]]
    if section[9]!=24 or section[5]%24:raise ValueError(f'Malformed ELF symbol table: {path}')
    result={}
    with path.open('rb') as stream:
        stream.seek(strings[4]);names=stream.read(strings[5])
        stream.seek(section[4]);table=stream.read(section[5])
        for pos in range(0,len(table),24):
            name,info,other,index,value,length=struct.unpack_from('<IBBHQQ',table,pos)
            if name>=len(names):raise ValueError('ELF symbol name outside string table')
            label=names[name:names.find(b'\0',name)].decode(errors='replace')
            if not label.startswith('__kstrtabns_'):continue
            if index>=len(elf['sections']):raise ValueError('Namespace symbol section outside table')
            data=elf['sections'][index]
            offset=value if elf['kind']==1 else value-data[3]
            if offset<0 or offset+length>data[5]:raise ValueError('Namespace symbol outside section')
            stream.seek(data[4]+offset)
            namespace=stream.read(length).split(b'\0',1)[0].decode()
            result[label[len('__kstrtabns_'):]]=namespace
    return result


def module_import_namespaces(path):
    return set(command(['modinfo','-F','import_ns',path]).decode().splitlines())


def namespace_failures(names,provided,imported):
    return [{'symbol':name,'namespace':provided[name]['namespace']}
            for name in sorted(names) if name in provided and provided[name].get('namespace')
            and provided[name]['namespace'] not in imported]


def exact_built_modules(module_root):
    paths=sorted(module_root.rglob('*.ko'))
    expected={'drivers/block/zram/zram.ko','mm/zsmalloc.ko'}
    found={str(path.relative_to(module_root)) for path in paths}
    if len(paths)!=2 or found!=expected:
        raise ValueError(f'Expected exactly {sorted(expected)} under {module_root}; found {sorted(found)}')
    return paths

def module_versions(path):
    result = {}
    for line in command(['modprobe', '--dump-modversions', path]).decode().splitlines():
        crc, symbol = line.split()
        if symbol in result:
            raise ValueError(f'Duplicate module version entry {path}: {symbol}')
        result[symbol] = int(crc, 16)
    if not result:
        raise ValueError(f'Module has no MODVERSIONS records: {path}')
    return result


def module_strong_imports(path):
    rows = command([NM, '--undefined-only', path]).decode().splitlines()
    return [parts[-1] for row in rows if (parts := row.split()) and parts[0] == 'U']


def protected_list(path):
    if not path.is_file():
        raise ValueError(f'Missing generated protection list {path}')
    return re.findall(r'^\s*"([A-Za-z0-9_.$]+)",?\s*$', path.read_text(), re.M)


def validate_signature(path, cert_der, temporary):
    data = path.read_bytes()
    marker = b'~Module signature appended~\n'
    if not data.endswith(marker):
        return {'valid': False, 'reason': 'module has no appended signature'}
    end = len(data) - len(marker)
    if end < 12:
        return {'valid': False, 'reason': 'truncated module signature header'}
    header = data[end - 12:end]
    length = struct.unpack('>I', header[8:])[0]
    if header[2] != 2 or header[3] != 0 or header[4] != 0 or length > end - 12:
        return {'valid': False, 'reason': 'unsupported or invalid module signature format'}
    (temporary / 'module-data').write_bytes(data[:end - 12 - length])
    (temporary / 'module-sig.der').write_bytes(data[end - 12 - length:end - 12])
    command(['openssl', 'x509', '-inform', 'DER', '-in', cert_der,
             '-out', temporary / 'build-cert.pem'])
    check = subprocess.run([
        'openssl', 'cms', '-verify', '-binary', '-inform', 'DER',
        '-in', str(temporary / 'module-sig.der'),
        '-content', str(temporary / 'module-data'),
        '-certfile', str(temporary / 'build-cert.pem'), '-nointern', '-noverify',
        '-out', '/dev/null',
    ], capture_output=True, text=True, timeout=30)
    return {'valid': check.returncode == 0,
            'scope': 'cryptographic signature verification against this build certificate; not public PKI trust',
            'openssl_output': check.stderr.strip()}


def verify(args):
    out = args.out_dir.resolve()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        'out_dir': str(out), 'expected_ksu': args.expected_ksu,
        'checks': {}, 'failures': [], 'full_abi_verified': False,
        'runtime_compatibility_verified': False, 'phone_operations': False,
        'limitations': [
            'CRC agreement does not prove full structure-layout/CFI/runtime compatibility.',
            'Stock module evidence comes from the verified downloaded official OTA; phone module bytes were not readable.',
            'Matching generated modules do not automatically replace modules already installed on a phone.',
            'No device is accessed and no boot compatibility is claimed.',
        ],
    }
    failures = report['failures']

    def check(name, fn):
        try:
            data = fn()
            report['checks'][name] = {'status': 'pass', 'result': data}
        except Exception as exc:
            message = f'{type(exc).__name__}: {exc}'
            report['checks'][name] = {'status': 'fail', 'error': message}
            failures.append(name + ': ' + message)

    def configuration():
        actual = config(out / '.config')
        stock = config(ROOT / 'configs/stock.config')
        expected = dict(REQUIRED_CONFIG)
        expected.update(json.loads((ROOT / 'configs/required-config.json').read_text()))
        expected['CONFIG_KSU'] = 'y' if args.expected_ksu == 'yes' else 'n'
        expected['CONFIG_KSU_SUSFS'] = expected['CONFIG_KSU']
        mismatches = [{'option': k, 'expected': v, 'actual': actual.get(k, 'n')}
                      for k, v in expected.items() if actual.get(k, 'n') != v]
        compiler = actual.get('CONFIG_CC_VERSION_TEXT', '')
        if 'r450784e' not in compiler or '14.0.7' not in compiler:
            mismatches.append({'option': 'CONFIG_CC_VERSION_TEXT', 'actual': compiler,
                               'expected': 'Clang r450784e / 14.0.7'})
        # No unrelated namespace/IPC feature expansion relative to stock.
        for key in ('CONFIG_SYSVIPC', 'CONFIG_POSIX_MQUEUE', 'CONFIG_IPC_NS',
                    'CONFIG_PID_NS', 'CONFIG_USER_NS', 'CONFIG_DEVTMPFS'):
            if actual.get(key, 'n') != stock.get(key, 'n'):
                mismatches.append({'option': key, 'actual': actual.get(key, 'n'),
                                   'expected': stock.get(key, 'n')})
        diff = [{'option': k, 'stock': stock.get(k, 'absent'),
                 'built': actual.get(k, 'absent')}
                for k in sorted(stock.keys() | actual.keys()) if stock.get(k) != actual.get(k)]
        (report_dir / 'config-comparison.json').write_text(json.dumps(diff, indent=2) + '\n')
        (report_dir / 'required-config-check.json').write_text(json.dumps({
            'required': expected, 'mismatches': mismatches, 'differences': len(diff),
        }, indent=2) + '\n')
        unexpected=[x for x in diff if x['option'] not in expected and x['option']!='CONFIG_UNUSED_KSYMS_WHITELIST']
        if mismatches or unexpected:
            raise ValueError(json.dumps({'required_mismatches':mismatches,'unexpected_drift':unexpected}))
        return {'differences_from_stock': len(diff), 'compiler': compiler,
                'stock_config_sha256': sha256(ROOT / 'configs/stock.config'),
                'built_config_sha256': sha256(out / '.config')}

    def source_identity():
        source_link = out / 'source'
        if not source_link.is_symlink():
            raise ValueError(f'Missing output source link: {source_link}')
        actual_source = source_link.resolve()
        if actual_source != (ROOT / 'kernel/common').resolve():
            raise ValueError(f'Output belongs to a different source checkout: {actual_source}')
        actual = command(['git', '-C', actual_source, 'rev-parse', 'HEAD']).decode().strip()
        if actual != EXPECTED_SOURCE:
            raise ValueError(f'Expected {EXPECTED_SOURCE}, got {actual}')
        return {'common_sha': actual, 'note': 'working-tree modifications are separately captured as source diffs'}

    def image_elf():
        image_path = out / 'arch/arm64/boot/Image'
        data = image_path.read_bytes()
        if data[56:60] != b'ARM\x64':
            raise ValueError('Image is not an arm64 kernel Image')
        addresses = {p[2]: int(p[0], 16) for line in (out / 'System.map').read_text().splitlines()
                     if len(p := line.split()) == 3}
        text_offset, memory_size, flags = struct.unpack_from('<QQQ', data, 8)
        if memory_size != addresses['_end'] - addresses['_text']:
            raise ValueError('Image header memory size differs from link symbols')
        if len(data) != addresses['_edata'] - addresses['_text']:
            raise ValueError('Image file size differs from link symbols')
        with (out / 'vmlinux').open('rb') as stream:
            elf = stream.read(64)
        if elf[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', elf, 18)[0] != 183:
            raise ValueError('vmlinux is not little-endian ELF64 AArch64')
        release = (out / 'include/config/kernel.release').read_text().strip()
        if args.expected_ksu == 'yes' and release != IDENTITY['kernel_release']:
            raise ValueError(f'Unexpected release {release}')
        embedded = command([ROOT / 'kernel/common/scripts/extract-ikconfig', image_path])
        if embedded != (out / '.config').read_bytes():
            raise ValueError('Image embedded configuration differs from final .config')
        elf_info=read_elf64(out/'vmlinux')
        with tempfile.TemporaryDirectory(prefix='image-identity-',dir=report_dir) as temporary:
            regenerated=Path(temporary)/'Image'
            command([NM.with_name('llvm-objcopy'),'-O','binary','-R','.note','-R','.note.gnu.build-id','-R','.comment','-S',out/'vmlinux',regenerated])
            if sha256(regenerated)!=sha256(image_path):raise ValueError('vmlinux does not regenerate the packaged Image')
            generated_map=Path(temporary)/'System.map'
            env_command=['env','NM='+str(NM),'sh',ROOT/'kernel/common/scripts/mksysmap',out/'vmlinux',generated_map]
            command(env_command)
            if generated_map.read_bytes()!=(out/'System.map').read_bytes():raise ValueError('System.map does not correspond to vmlinux')
        if args.expected_ksu == 'yes':
            if not any('ksu_' in k for k in addresses) or not any('susfs_' in k for k in addresses):
                raise ValueError('Missing SukiSU/SUSFS symbols in System.map')
            if IDENTITY['driver_version_full'].encode() not in data:raise ValueError('Image lacks expected real builtin release identity')
            if IDENTITY['susfs_version'].encode() not in data:raise ValueError('Image lacks locked SUSFS version')
            commands=list((out/'drivers/kernelsu').glob('.*.cmd'))
            if not commands or not any(('-DKSU_VERSION=' + str(IDENTITY['driver_version'])).encode() in item.read_bytes() for item in commands):raise ValueError('Actual driver compilation did not use the locked version')
        return {'Image_sha256': sha256(image_path), 'Image_bytes': len(data),
                'memory_size': memory_size, 'text_offset': text_offset, 'flags': flags,
                'release': release, 'embedded_config_matches': True, 'vmlinux_architecture': 'AArch64',
                'ELF_complete':True,'Image_regenerated_from_ELF':True,'System_map_matches_ELF':True}

    check('configuration', configuration)
    check('source_identity', source_identity)
    if args.expected_ksu == 'yes':
        def provenance():
            spec = importlib.util.spec_from_file_location('verify_build_provenance', ROOT / 'scripts/build_provenance.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            record = module.verify_workspace(ROOT, out)
            return {'attempt': record['attempt'], 'input_output_binding': True}
        check('build_input_output_provenance', provenance)
    check('image_and_elf', image_elf)
    try:
        built = symvers(out / 'Module.symvers')
    except Exception as exc:
        failures.append(f'Module.symvers: {exc}')
        built = {}

    def baseline_exports():
        baseline=symvers(ROOT / 'inputs/baseline/Module.symvers')
        missing=sorted(baseline.keys()-built.keys())
        changed={name:{'baseline':baseline[name],'built':built[name]} for name in baseline.keys()&built.keys() if baseline[name]!=built[name]}
        if missing or changed:raise ValueError(json.dumps({'missing':missing,'changed':changed}))
        return {'baseline':len(baseline),'built':len(built),'namespace_compared':True}

    check('baseline_exports_including_namespaces',baseline_exports)

    def stock_modules():
        evidence_path = STOCK / 'device-check/full-audit/complete-crc-comparison.json'
        export_path = STOCK / 'device-check/full-audit/stock-module-export-crcs.json'
        evidence = json.loads(evidence_path.read_text())['details']
        peer_exports = json.loads(export_path.read_text())
        by_path = {p: x['sha256'] for x in evidence for p in x['paths']}
        # Validate provenance before reusing provider CRC evidence.
        for relative, expected in by_path.items():
            if sha256(STOCK / relative) != expected:
                raise ValueError(f'Stock module changed: {relative}')
        jobs = []
        for x in evidence:
            jobs.append((x['paths'][0], x['sha256']))

        def inspect(job):
            relative, digest = job
            path = STOCK / relative
            versions = module_versions(path)
            imports = module_strong_imports(path)
            imported_ns=module_import_namespaces(path)
            namespace_errors=namespace_failures(set(imports)|set(versions),built,imported_ns)
            for name in set(imports)|set(versions):
                if name in built or name not in peer_exports:continue
                for provider in peer_exports[name]:
                    namespace=module_export_namespaces(STOCK/provider['module']).get(name,'')
                    if namespace and namespace not in imported_ns:
                        namespace_errors.append({'symbol':name,'namespace':namespace,'provider':provider['module']})
            direct = peer = 0
            mismatch, missing, ambiguity = [], [], []
            for name, required_crc in versions.items():
                if name in built:
                    if built[name]['crc'] == required_crc:
                        direct += 1
                    else:
                        mismatch.append({'symbol': name, 'required': hex(required_crc),
                                         'provided': hex(built[name]['crc']), 'provider': built[name]['provider']})
                elif name in peer_exports:
                    values = {x['crc'] for x in peer_exports[name]}
                    if len(values) != 1:
                        ambiguity.append(name)
                    elif required_crc in values:
                        peer += 1
                    else:
                        mismatch.append({'symbol': name, 'required': hex(required_crc),
                                         'provided': [hex(x) for x in values], 'provider': 'retained stock modules'})
                else:
                    missing.append(name)
            unresolved = [x for x in imports if x not in built and x not in peer_exports]
            return {'module': relative, 'sha256': digest, 'version_records': len(versions),
                    'matching_built_crcs': direct, 'matching_stock_provider_crcs': peer,
                    'crc_mismatches': mismatch, 'missing_crc_providers': missing,
                    'ambiguous_crc_providers': ambiguity, 'unresolved_strong_imports': unresolved,'namespace_import_failures':namespace_errors}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(inspect, jobs))
        summary = {'unique_modules': len(results), 'module_paths': len(by_path),
                   'version_records': sum(x['version_records'] for x in results),
                   'matching_built_crcs': sum(x['matching_built_crcs'] for x in results),
                   'matching_stock_provider_crcs': sum(x['matching_stock_provider_crcs'] for x in results)}
        for field in ('crc_mismatches', 'missing_crc_providers', 'ambiguous_crc_providers', 'unresolved_strong_imports','namespace_import_failures'):
            summary[field] = sum(len(x[field]) for x in results)
        (report_dir / 'stock-module-crc-comparison.json').write_text(json.dumps({
            'summary': summary, 'details': results, 'full_abi_verified': False,
            'runtime_verified': False, 'cached_provider_evidence_sha256': sha256(export_path),
            'cached_module_manifest_sha256': sha256(evidence_path),
        }, indent=2) + '\n')
        if not results or any(summary[k] for k in ('crc_mismatches', 'missing_crc_providers',
                                                  'ambiguous_crc_providers', 'unresolved_strong_imports','namespace_import_failures')):
            raise ValueError(json.dumps(summary))
        return summary

    def built_modules():
        release = (out / 'include/config/kernel.release').read_text().strip()
        staging = args.staging_dir.resolve() if args.staging_dir else out.parent / 'staging'
        module_root = staging / 'lib/modules' / release / 'kernel'
        paths = exact_built_modules(module_root)
        cert = out / 'certs/signing_key.x509'
        results = []
        with tempfile.TemporaryDirectory(prefix='module-verify-', dir=report_dir) as temporary:
            for path in paths:
                versions = module_versions(path)
                missing = [x for x in versions if x not in built]
                mismatch = [x for x, crc in versions.items() if x in built and built[x]['crc'] != crc]
                strong_imports=module_strong_imports(path)
                unresolved = [x for x in strong_imports if x not in built]
                namespace_errors=namespace_failures(set(versions)|set(strong_imports),built,module_import_namespaces(path))
                signature = validate_signature(path, cert, Path(temporary))
                results.append({'module': str(path.relative_to(staging)), 'sha256': sha256(path),
                                'version_records': len(versions), 'missing_crc_providers': missing,
                                'crc_mismatches': mismatch, 'unresolved_strong_imports': unresolved,
                                'signature': signature,'namespace_import_failures':namespace_errors})
        (report_dir / 'built-module-checks.json').write_text(json.dumps(results, indent=2) + '\n')
        if any(x['missing_crc_providers'] or x['crc_mismatches'] or x['unresolved_strong_imports']
               or x['namespace_import_failures'] or not x['signature']['valid'] for x in results):
            raise ValueError('Built module import/CRC/signature verification failed; see built-module-checks.json')
        return {'modules': len(results), 'public_certificate_sha256': sha256(cert),
                'signature_scope': 'this-build installed modules against this-build certificate'}

    def signature_policy():
        protected = protected_list(out / 'kernel/gki_module_protected.h')
        reserved = protected_list(out / 'kernel/gki_module_exported.h')
        return {'protected_import_symbols': protected, 'reserved_export_symbols': reserved,
                'stock_modules_trusted_by_build_certificate': 'not established',
                'stock_signature_outcome': 'unknown keys may be provisionally accepted under MODULE_SIG_PROTECT; actual protection lists and module loader rules still apply',
                'runtime_load_test': 'not performed'}

    check('stock_module_crcs_and_imports', stock_modules)
    check('built_modules_and_signatures', built_modules)
    check('signature_policy', signature_policy)
    report['passed'] = not failures
    (report_dir / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    summary = {'passed': report['passed'], 'expected_ksu': args.expected_ksu,
               'failed_checks': failures, 'report': str(report_dir / 'verification.json'),
               'full_abi_verified': False, 'runtime_compatibility_verified': False}
    print(json.dumps(summary, indent=2))
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', required=True, type=Path,
                        help='build/build.sh common output directory containing .config and Module.symvers')
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--staging-dir', type=Path,
                        help='Optional modules staging root; default OUT_DIR/../staging')
    parser.add_argument('--expected-ksu', required=True, choices=('yes', 'no'))
    args=parser.parse_args()
    if os.environ.get('FUXI_PIPELINE_LOCK_HELD')=='1':
        return verify(args)
    with (ROOT/'.pipeline.lock').open('a') as pipeline_lock:
        try:fcntl.flock(pipeline_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('The build/delivery pipeline is busy; retry after it finishes.',file=sys.stderr)
            return 2
        return verify(args)


if __name__ == '__main__':
    sys.exit(main())
