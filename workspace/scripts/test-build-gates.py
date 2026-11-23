#!/usr/bin/env python3
"""Isolated regression tests for the historical build/verification false positives."""
from pathlib import Path
import importlib.util,json,subprocess,tempfile
ROOT=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('verify_build',ROOT/'scripts/verify-build.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
config_spec=importlib.util.spec_from_file_location('check_config',ROOT/'scripts/check-config.py')
configuration=importlib.util.module_from_spec(config_spec);config_spec.loader.exec_module(configuration)
results=[]
def passed(name):
 results.append({'check':name,'passed':True});print('PASS:',name)
def rejects(fn,name):
 try:fn()
 except (ValueError,KeyError):passed(name)
 else:raise AssertionError('Gate did not reject: '+name)
with tempfile.TemporaryDirectory(prefix='fuxi-build-gates-') as temp:
 root=Path(temp)
 # A 64-byte copied ELF header used to pass the entire ELF check.
 original=ROOT/'out/sukisu/common/vmlinux'
 truncated=root/'truncated-vmlinux'
 with original.open('rb') as stream:truncated.write_bytes(stream.read(64))
 rejects(lambda:module.read_elf64(truncated),'truncated ELF header rejected')
 valid=module.read_elf64(original)
 assert valid['bytes']>64;passed('complete existing AArch64 ELF accepted')
 symbols=root/'Module.symvers'
 symbols.write_text('0x12345678\tmemcpy\tvmlinux\tEXPORT_SYMBOL\tREVIEW_NAMESPACE\n')
 exports=module.symvers(symbols)
 assert exports['memcpy']['namespace']=='REVIEW_NAMESPACE';passed('export namespace retained')
 assert module.namespace_failures({'memcpy'},exports,set())
 assert not module.namespace_failures({'memcpy'},exports,{'REVIEW_NAMESPACE'})
 passed('unimported namespace rejected and declared namespace accepted')
 module_root=root/'modules'
 for relative in ('drivers/block/zram/zram.ko','mm/zsmalloc.ko'):
  path=module_root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'test fixture')
 assert len(module.exact_built_modules(module_root))==2;passed('exact expected module paths accepted')
 duplicate=module_root/'extra/zram.ko';duplicate.parent.mkdir();duplicate.write_bytes(b'test duplicate')
 rejects(lambda:module.exact_built_modules(module_root),'duplicate module rejected before packaging')
 duplicate.unlink();(module_root/'mm/zsmalloc.ko').unlink()
 rejects(lambda:module.exact_built_modules(module_root),'missing module rejected')
 old=ROOT/'out/sukisu/common/.config'
 good=root/'good.config';good.write_bytes(old.read_bytes())
 configuration.validate(good,ROOT/'configs/stock.config')
 passed('prior hardened SUSFS feature configuration accepted')
 bad=root/'bad.config';bad.write_text(good.read_text().replace('CONFIG_KSU_SUSFS_OPEN_REDIRECT=y','# CONFIG_KSU_SUSFS_OPEN_REDIRECT is not set'))
 rejects(lambda:configuration.validate(bad,ROOT/'configs/stock.config'),'missing SUSFS subfeature rejected')
 bad.write_text(good.read_text().replace('CONFIG_CFI_CLANG=y','# CONFIG_CFI_CLANG is not set'))
 rejects(lambda:configuration.validate(bad,ROOT/'configs/stock.config'),'disabled stock CFI rejected')
 subprocess.run(['bash','-n',ROOT/'scripts/run-build.sh'],check=True)
 passed('launcher Bash syntax')
report={'passed':True,'checks':results,'real_kernel_build_executed':False,'device_operations':False}
(ROOT/'reports/build-gate-tests.json').write_text(json.dumps(report,indent=2)+'\n')
